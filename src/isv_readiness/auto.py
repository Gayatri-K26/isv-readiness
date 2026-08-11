from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from isv_readiness.change_verification import apply_verified_change_set, verify_change_set
from isv_readiness.changes import ChangeSet
from isv_readiness.context import ContextError, build_context_pack, provider_contract_constraints
from isv_readiness.decision import adapter_contract_unit, decide_gap, validation_profile_issues
from isv_readiness.domain_audit import (
    DomainAuditError,
    approved_test_capabilities,
    audit_gap_rows,
    merge_audit_rows,
    run_domain_audit,
    write_domain_audit,
)
from isv_readiness.failure_feedback import (
    MAX_FAILURE_DETAIL_CHARS,
    MAX_FAILURE_DETAILS,
    MAX_FAILURE_SUMMARY_CHARS,
    artifact_reference,
    bounded_failure_text,
    redact_failure_text,
    redact_failure_value,
    stable_failure_fingerprint,
)
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.generation import GeneratorInfrastructureError, GeneratorRunner, run_generator
from isv_readiness.generator_limits import GENERATOR_ADAPTER_TIMEOUT_SECONDS
from isv_readiness.generators import (
    DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
    GeneratorExchangeError,
    GeneratorRequestExported,
)
from isv_readiness.project import ReadinessProject, declared_provider_environment, load_project
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import canonicalize_domain, load_solution_profile

AUTO_REVIEW_VERSION = "0.1.0"


class AutoWorkflowError(ValueError):
    """Raised when the autonomous fill-and-fix flow cannot proceed safely."""


@dataclass(frozen=True)
class StagedFix:
    gap_id: str
    target: str
    validation_class: str | None
    summary: str
    attempts: int


@dataclass(frozen=True)
class AttemptFailure:
    attempt: int
    category: str
    fingerprint: str
    summary: str
    details: tuple[str, ...]
    expected: str
    actual: str
    stable_error: str
    representative_excerpt: str
    affected_checks: tuple[str, ...]
    affected_count: int
    artifact_refs: tuple[dict[str, Any], ...]
    retryable: bool
    retry_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParkedGap:
    gap_id: str
    status: str
    action: str
    reason: str
    masked_failure: bool


@dataclass(frozen=True)
class ChangedFile:
    path: str
    before_sha256: str | None
    after_sha256: str
    creates_file: bool


@dataclass(frozen=True)
class AutoReview:
    schema_version: str
    domain: str
    status: str  # "no_changes" | "awaiting_review" | "applied"
    patch: str
    patch_sha256: str
    staged: tuple[StagedFix, ...]
    parked: tuple[ParkedGap, ...]
    changed_files: tuple[ChangedFile, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("staged", "parked", "changed_files"):
            payload[key] = [asdict(item) for item in getattr(self, key)]
        return payload


def run_auto(
    project_path: Path,
    *,
    domain: str,
    work_dir: Path,
    generator_command: Sequence[str],
    generator_pass_env: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    generator_runner: GeneratorRunner | None = None,
    generator_timeout_seconds: int = GENERATOR_ADAPTER_TIMEOUT_SECONDS,
    generator_idle_timeout_seconds: int | None = None,
    generator_max_request_bytes: int = DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
    max_generator_calls: int | None = None,
    max_iterations: int = 50,
    apply: bool = False,
    approval_patch_sha256: str | None = None,
) -> AutoReview:
    """Fill and fix every ISV-owned, auto-fixable gap in one domain, then stop.

    Each candidate is generated, verified in isolation, and staged into a private
    scratch copy of the provider — the real repository is never touched until an
    explicit, hash-bound ``apply``. The result is a single combined diff (one
    review gate) plus the list of gaps parked for a human scope decision.
    """
    project_path = project_path.expanduser().resolve()
    project = load_project(project_path)
    canonical_domain = canonicalize_domain(domain)
    if canonical_domain not in project.assessment.domains:
        raise AutoWorkflowError(f"Domain '{canonical_domain}' is outside the ISV-owned project scope.")
    provider_root = project.provider_root(project_path)
    if not provider_root.is_dir():
        raise AutoWorkflowError("Provider is not scaffolded; create a new workspace with `gapctl init`.")
    if not generator_command:
        raise AutoWorkflowError("An explicit generator adapter is required (--generator).")
    if max_generator_calls is not None and max_generator_calls < 1:
        raise AutoWorkflowError("max_generator_calls must be positive when supplied.")

    profile = _load_profile(project, project_path)
    profile_issues = validation_profile_issues(profile, [canonical_domain])
    if profile_issues:
        raise AutoWorkflowError("Validation profile is not ready: " + "; ".join(profile_issues))

    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = work_dir / "scratch-provider"
    scratch_backups = work_dir / "scratch-backups"

    if apply:
        # Apply is an apply-ONLY action: it consumes the previously staged
        # scratch exactly as reviewed. It never regenerates — a regenerated
        # patch could not match the reviewed hash anyway (models are not
        # byte-deterministic), and silently redoing 10 model calls before
        # refusing would waste both time and trust.
        return _apply_reviewed_scratch(
            provider_root,
            scratch,
            work_dir,
            domain=canonical_domain,
            approval_patch_sha256=approval_patch_sha256,
        )

    _discard_review_artifacts(work_dir)
    for stale in (scratch, scratch_backups):
        if stale.exists():
            shutil.rmtree(stale)
    shutil.copytree(provider_root, scratch)

    staged: list[StagedFix] = []
    attempts_by_unit: dict[str, int] = {}
    feedback_by_unit: dict[str, list[AttemptFailure]] = {}
    blocked_by_unit: dict[str, str] = {}
    generator_calls = 0
    max_attempts = project.execution.max_attempts
    allowed_environment = declared_provider_environment(project, canonical_domain)
    audit_phase = "not_run"
    active_audit_rows = []
    semantic_candidate_staged = False
    post_audit_pending_reason: str | None = None
    audit_required = bool(profile and approved_test_capabilities(profile, canonical_domain))

    for _ in range(max_iterations):
        report = _scan(scratch, project.validation_root(project_path), canonical_domain, profile)
        if active_audit_rows:
            report = merge_audit_rows(report, active_audit_rows)
        fixable = _select_fixable(report, canonical_domain)
        # Drop gaps already staged (a re-scan should show them resolved; if it
        # does not, the retry budget below stops an infinite loop).
        pending = [
            row
            for row in fixable
            if attempts_by_unit.get(adapter_contract_unit(row), 0) < max_attempts
        ]
        if not pending:
            # Exhausted or non-editable rows remain parked; an audit cannot make
            # a rejected deterministic candidate safe or override scope.
            if fixable or active_audit_rows:
                break
            if not audit_required:
                break
            if audit_phase == "not_run":
                if max_generator_calls is not None and generator_calls >= max_generator_calls:
                    break
                active_audit_rows = _run_domain_completeness_audit(
                    project,
                    project_path,
                    report,
                    profile,
                    domain=canonical_domain,
                    scratch=scratch,
                    work_dir=work_dir,
                    generator_command=generator_command,
                    generator_pass_env=generator_pass_env,
                    environment=environment,
                    generator_runner=generator_runner,
                    generator_timeout_seconds=generator_timeout_seconds,
                    generator_idle_timeout_seconds=generator_idle_timeout_seconds,
                    generator_max_request_bytes=generator_max_request_bytes,
                    artifact_path=work_dir / "domain-audit.pre.json",
                )
                generator_calls += 1
                audit_phase = "pre"
                if active_audit_rows:
                    continue
                break
            if audit_phase == "pre" and semantic_candidate_staged:
                if max_generator_calls is not None and generator_calls >= max_generator_calls:
                    post_audit_pending_reason = (
                        "The generator-call limit was reached before the required post-change "
                        "domain completeness audit. Re-run auto to obtain semantic confirmation."
                    )
                    break
                active_audit_rows = _run_domain_completeness_audit(
                    project,
                    project_path,
                    report,
                    profile,
                    domain=canonical_domain,
                    scratch=scratch,
                    work_dir=work_dir,
                    generator_command=generator_command,
                    generator_pass_env=generator_pass_env,
                    environment=environment,
                    generator_runner=generator_runner,
                    generator_timeout_seconds=generator_timeout_seconds,
                    generator_idle_timeout_seconds=generator_idle_timeout_seconds,
                    generator_max_request_bytes=generator_max_request_bytes,
                    artifact_path=work_dir / "domain-audit.post.json",
                )
                generator_calls += 1
                audit_phase = "post"
            break
        if max_generator_calls is not None and generator_calls >= max_generator_calls:
            break
        row = pending[0]
        gap_id = row["id"]
        contract_unit = adapter_contract_unit(row)

        try:
            change_set, contract_constraints = _generate(
                project,
                project_path,
                report,
                gap_id=gap_id,
                scratch=scratch,
                work_dir=work_dir,
                generator_command=generator_command,
                generator_pass_env=generator_pass_env,
                environment=environment,
                generator_runner=generator_runner,
                generator_timeout_seconds=generator_timeout_seconds,
                generator_idle_timeout_seconds=generator_idle_timeout_seconds,
                generator_max_request_bytes=generator_max_request_bytes,
                feedback=feedback_by_unit.get(contract_unit, ()),
            )
            generator_calls += 1
            if not change_set.changes:
                attempts_by_unit[contract_unit] = max_attempts
                blocked_by_unit[contract_unit] = (
                    "Generator reported no source-grounded provider-owned implementation: "
                    f"{change_set.summary}"
                )
                if max_generator_calls is not None and generator_calls >= max_generator_calls:
                    break
                continue
            manifest = verify_change_set(
                report,
                provider_repo=scratch,
                change_set=change_set,
                validation_root=project.validation_root(project_path),
                allowed_environment=allowed_environment,
                contract_constraints=contract_constraints,
            )
        except (GeneratorRequestExported, GeneratorExchangeError):
            raise
        except GeneratorInfrastructureError as exc:
            raise AutoWorkflowError(
                "Generator infrastructure failed; validation stopped without changing the real provider: "
                f"{exc}"
            ) from exc
        except ContextError as exc:
            attempts_by_unit[contract_unit] = max_attempts
            blocked_by_unit[contract_unit] = (
                "Automatic regeneration was parked because the required failure evidence "
                f"could not be represented safely: {exc}"
            )
            continue
        except FixGuardrailError as exc:
            # A malformed generation or guard violation is a failed attempt for
            # this gap, not a reason to abort every other gap in the run.
            attempts_by_unit[contract_unit] = attempts_by_unit.get(contract_unit, 0) + 1
            failure_artifact = _write_failure_artifact(
                work_dir,
                contract_unit=contract_unit,
                attempt=attempts_by_unit[contract_unit],
                category="guardrail",
                payload={"error": str(exc)},
            )
            stop_reason = _record_failure(
                feedback_by_unit,
                contract_unit,
                attempt=attempts_by_unit[contract_unit],
                category="guardrail",
                summary="Candidate was rejected by a deterministic guardrail.",
                details=(str(exc),),
                expected="A schema-valid candidate confined to the reviewed provider-owned edit scope.",
                actual=str(exc),
                affected_check=gap_id,
                artifact_refs=(artifact_reference("failure", failure_artifact),),
                max_failure_groups=project.execution.max_failure_groups,
            )
            if stop_reason:
                attempts_by_unit[contract_unit] = max_attempts
                blocked_by_unit[contract_unit] = stop_reason
            continue
        attempts_by_unit[contract_unit] = attempts_by_unit.get(contract_unit, 0) + 1
        if not manifest.success:
            # Leave the gap for the next iteration's retry budget; a fresh scan
            # keeps selecting it until the budget is exhausted, then it parks.
            details = (
                *manifest.selected_failure_details,
                *(f"Regression: {item}" for item in manifest.regressions),
            )
            failure_artifact = _write_failure_artifact(
                work_dir,
                contract_unit=contract_unit,
                attempt=attempts_by_unit[contract_unit],
                category="static_verification",
                payload={
                    "change_set": change_set.to_dict(),
                    "verification": manifest.to_dict(),
                },
            )
            stop_reason = _record_failure(
                feedback_by_unit,
                contract_unit,
                attempt=attempts_by_unit[contract_unit],
                category="static_verification",
                summary=(
                    "Candidate failed isolated static verification; selected status became "
                    f"{manifest.selected_status_after or 'missing'}."
                ),
                details=details,
                expected="The selected check passes isolated static verification without regressions.",
                actual=(
                    f"Selected status was {manifest.selected_status_after or 'missing'} "
                    f"with {len(manifest.regressions)} regression(s)."
                ),
                affected_check=gap_id,
                artifact_refs=(artifact_reference("failure", failure_artifact),),
                max_failure_groups=project.execution.max_failure_groups,
            )
            if stop_reason:
                attempts_by_unit[contract_unit] = max_attempts
                blocked_by_unit[contract_unit] = stop_reason
            continue
        apply_verified_change_set(
            report,
            provider_repo=scratch,
            change_set=change_set,
            manifest=manifest,
            backup_dir=scratch_backups,
            allowed_environment=allowed_environment,
        )
        staged.append(
            StagedFix(
                gap_id=gap_id,
                target=str(row.get("remediation", {}).get("target") or ""),
                validation_class=row.get("validation_class"),
                summary=change_set.summary,
                attempts=attempts_by_unit[contract_unit],
            )
        )
        if row.get("detection") == "semantic":
            active_audit_rows = [
                item for item in active_audit_rows if item.id != gap_id
            ]
            semantic_candidate_staged = True
        if max_generator_calls is not None and generator_calls >= max_generator_calls:
            break

    final_report = _scan(scratch, project.validation_root(project_path), canonical_domain, profile)
    if active_audit_rows:
        final_report = merge_audit_rows(final_report, active_audit_rows)
    parked = _park(
        final_report,
        canonical_domain,
        staged,
        attempts_by_unit,
        max_attempts,
        blocked_by_unit=blocked_by_unit,
        feedback_by_unit=feedback_by_unit,
    )
    if post_audit_pending_reason:
        parked.append(
            ParkedGap(
                gap_id="domain-audit-post",
                status="error",
                action="implement_or_fix_adapter",
                reason=post_audit_pending_reason,
                masked_failure=False,
            )
        )
    changed_files = _changed_files(provider_root, scratch)
    patch = _combined_patch(provider_root, scratch, changed_files)
    patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()

    if not changed_files:
        review = AutoReview(
            schema_version=AUTO_REVIEW_VERSION,
            domain=canonical_domain,
            status="no_changes",
            patch="",
            patch_sha256=patch_sha256,
            staged=tuple(staged),
            parked=tuple(parked),
            changed_files=(),
            reason="No auto-fixable owned gaps produced a verified change.",
        )
        _write_review(work_dir, review)
        return review

    review = AutoReview(
        schema_version=AUTO_REVIEW_VERSION,
        domain=canonical_domain,
        status="awaiting_review",
        patch=patch,
        patch_sha256=patch_sha256,
        staged=tuple(staged),
        parked=tuple(parked),
        changed_files=tuple(changed_files),
        reason=(
            f"{len(staged)} statically verified candidate(s) staged for one review; "
            f"{len(parked)} gap(s) parked. "
            "Review the combined patch and re-run with --apply <patch-sha256>."
        ),
    )
    _write_review(work_dir, review)
    return review


def _apply_reviewed_scratch(
    provider_root: Path,
    scratch: Path,
    work_dir: Path,
    *,
    domain: str,
    approval_patch_sha256: str | None,
) -> AutoReview:
    """Apply the previously staged scratch exactly as reviewed — no regeneration."""
    if not scratch.is_dir():
        raise AutoWorkflowError(
            "No staged run to apply; run auto without --apply first to stage and review fixes."
        )
    changed_files = _changed_files(provider_root, scratch)
    patch = _combined_patch(provider_root, scratch, changed_files)
    patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    if not changed_files:
        return AutoReview(
            schema_version=AUTO_REVIEW_VERSION,
            domain=domain,
            status="no_changes",
            patch="",
            patch_sha256=patch_sha256,
            staged=(),
            parked=(),
            changed_files=(),
            reason="Staged scratch matches the provider; nothing to apply (already applied?).",
        )
    if approval_patch_sha256 != patch_sha256:
        return AutoReview(
            schema_version=AUTO_REVIEW_VERSION,
            domain=domain,
            status="awaiting_review",
            patch=patch,
            patch_sha256=patch_sha256,
            staged=(),
            parked=(),
            changed_files=tuple(changed_files),
            reason=(
                "Apply refused: supplied approval hash does not match the staged patch "
                f"({patch_sha256[:12]}…). Review auto-review.patch and pass the exact hash."
            ),
        )
    _apply_to_provider(provider_root, scratch, changed_files, work_dir / "backups")
    review = AutoReview(
        schema_version=AUTO_REVIEW_VERSION,
        domain=domain,
        status="applied",
        patch=patch,
        patch_sha256=patch_sha256,
        staged=(),
        parked=(),
        changed_files=tuple(changed_files),
        reason=f"Applied {len(changed_files)} file(s) atomically after hash-bound review approval.",
    )
    _write_review(work_dir, review)
    return review


def _select_fixable(report: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    # The scanner emits rows in the normalized provider execution order.
    # Preserve it: setup producers must be handled before dependent test and
    # teardown adapters, regardless of target size or alphabetical name.
    return [
        row
        for row in report.get("rows", [])
        if row.get("domain") == domain
        and decide_gap(row).edit_eligible
    ]


def _park(
    report: dict[str, Any],
    domain: str,
    staged: Sequence[StagedFix],
    attempts_by_unit: Mapping[str, int] | None = None,
    max_attempts: int = 0,
    *,
    blocked_by_unit: Mapping[str, str] | None = None,
    feedback_by_unit: Mapping[str, Sequence[AttemptFailure | str]] | None = None,
) -> list[ParkedGap]:
    staged_ids = {fix.gap_id for fix in staged}
    attempts_by_unit = attempts_by_unit or {}
    blocked_by_unit = blocked_by_unit or {}
    feedback_by_unit = feedback_by_unit or {}
    continue_instruction = (
        "apply the staged patch and re-run auto" if staged else "re-run auto"
    )
    parked: list[ParkedGap] = []
    for row in report.get("rows", []):
        decision = decide_gap(row)
        if row.get("domain") != domain or not decision.blocking:
            continue
        if row.get("id") in staged_ids:
            continue
        action = decision.action
        if decision.edit_eligible:
            contract_unit = adapter_contract_unit(row)
            attempts = attempts_by_unit.get(contract_unit, 0)
            blocker = blocked_by_unit.get(contract_unit)
            last_feedback = _latest_failure_text(feedback_by_unit.get(contract_unit, ()))
            if blocker:
                reason = blocker
            elif attempts == 0:
                reason = (
                    "Not attempted within this run's iteration budget; "
                    f"{continue_instruction} to continue."
                )
            elif attempts < max_attempts:
                reason = (
                    f"{attempts} failed attempt(s); retry budget remains — "
                    f"{continue_instruction}."
                )
                if last_feedback:
                    reason += f" Last failure: {last_feedback}"
            else:
                reason = "Auto-fix attempts were exhausted without a verified candidate."
                if last_feedback:
                    reason += f" Last failure: {last_feedback}"
        elif action == "implement_or_fix_adapter":
            # e.g. an unwired step: the fix is a config/scope decision, and the
            # deterministic scanner refuses to authorize an automatic edit.
            reason = (
                "Scanner did not mark this row safely auto-fixable "
                f"(evidence: {str((row.get('evidence') or {}).get('message') or '')[:80]}); "
                "a human config or scope decision is required."
            )
        else:
            reason = f"Requires human route '{action}' before code can be generated."
        sp = (row.get("enrichment") or {}).get("solution_profile") or {}
        reconciliation = sp.get("reconciliation") or {}
        parked.append(
            ParkedGap(
                gap_id=str(row.get("id")),
                status=str(row.get("status")),
                action=action,
                reason=reason,
                masked_failure=bool(reconciliation.get("masked_failure")),
            )
        )
    parked.sort(key=lambda item: item.gap_id)
    return parked


def _generate(
    project: ReadinessProject,
    project_path: Path,
    report: dict[str, Any],
    *,
    gap_id: str,
    scratch: Path,
    work_dir: Path,
    generator_command: Sequence[str],
    generator_pass_env: Sequence[str],
    environment: Mapping[str, str] | None,
    generator_runner: GeneratorRunner | None,
    generator_timeout_seconds: int,
    generator_idle_timeout_seconds: int | None,
    generator_max_request_bytes: int,
    feedback: Sequence[AttemptFailure],
) -> tuple[ChangeSet, dict[str, float]]:
    context_pack = build_context_pack(
        project,
        project_path,
        report,
        gap_id=gap_id,
        cache_dir=project_path.parent / ".gapctl" / "context-cache",
        environment=environment,
        feedback=[failure.to_dict() for failure in feedback],
        provider_root_override=scratch,
    )
    raw_context_pack = context_pack.to_dict()
    return run_generator(
        raw_context_pack,
        command=list(generator_command),
        cwd=work_dir,
        pass_env=generator_pass_env,
        timeout_seconds=generator_timeout_seconds,
        idle_timeout_seconds=generator_idle_timeout_seconds,
        max_request_bytes=generator_max_request_bytes,
        runner=generator_runner,
        environment=environment,
        protected_roots=(
            project_path,
            project.validation_root(project_path),
            project.provider_root(project_path),
            scratch,
            *(
                (project.resolve_path(project_path, project.assessment.profile),)
                if project.assessment.profile
                else ()
            ),
        ),
    ), provider_contract_constraints(raw_context_pack)


def _run_domain_completeness_audit(
    project: ReadinessProject,
    project_path: Path,
    report: dict[str, Any],
    profile,
    *,
    domain: str,
    scratch: Path,
    work_dir: Path,
    generator_command: Sequence[str],
    generator_pass_env: Sequence[str],
    environment: Mapping[str, str] | None,
    generator_runner: GeneratorRunner | None,
    generator_timeout_seconds: int,
    generator_idle_timeout_seconds: int | None,
    generator_max_request_bytes: int,
    artifact_path: Path,
):
    try:
        audit, approved_capabilities = run_domain_audit(
            project,
            project_path,
            report,
            profile,
            domain=domain,
            provider_repo=scratch,
            work_dir=work_dir,
            command=generator_command,
            pass_env=generator_pass_env,
            environment=environment,
            runner=generator_runner,
            timeout_seconds=generator_timeout_seconds,
            idle_timeout_seconds=generator_idle_timeout_seconds,
            max_request_bytes=generator_max_request_bytes,
        )
        write_domain_audit(artifact_path, audit)
        return audit_gap_rows(
            audit,
            approved_capabilities,
            profile=profile,
            provider_repo=scratch,
            report=report,
        )
    except (GeneratorRequestExported, GeneratorExchangeError):
        raise
    except GeneratorInfrastructureError as exc:
        raise AutoWorkflowError(
            "Domain-audit infrastructure failed; validation stopped without changing the real provider: "
            f"{exc}"
        ) from exc
    except (ContextError, DomainAuditError, FixGuardrailError) as exc:
        raise AutoWorkflowError(f"Domain completeness audit failed closed: {exc}") from exc


def _record_failure(
    failures_by_unit: dict[str, list[AttemptFailure]],
    contract_unit: str,
    *,
    attempt: int,
    category: str,
    summary: str,
    details: tuple[str, ...],
    expected: str,
    actual: str,
    affected_check: str,
    artifact_refs: Sequence[dict[str, Any] | None],
    max_failure_groups: int,
) -> str | None:
    normalized_summary = " ".join(redact_failure_text(summary).split())
    normalized_details = tuple(
        " ".join(redact_failure_text(detail).split())
        for detail in details
    )
    bounded_details = tuple(
        bounded_failure_text(detail, MAX_FAILURE_DETAIL_CHARS)
        for detail in normalized_details[:MAX_FAILURE_DETAILS]
    )
    if len(normalized_details) > MAX_FAILURE_DETAILS:
        omitted = json.dumps(normalized_details[MAX_FAILURE_DETAILS:], separators=(",", ":"))
        bounded_details += (
            f"{len(normalized_details) - MAX_FAILURE_DETAILS} additional detail(s) omitted "
            f"(sha256 {hashlib.sha256(omitted.encode('utf-8')).hexdigest()}).",
        )
    failure = AttemptFailure(
        attempt=attempt,
        category=category,
        fingerprint=stable_failure_fingerprint(category, normalized_summary, normalized_details),
        summary=bounded_failure_text(normalized_summary, MAX_FAILURE_SUMMARY_CHARS),
        details=bounded_details,
        expected=bounded_failure_text(expected, MAX_FAILURE_DETAIL_CHARS),
        actual=bounded_failure_text(actual, MAX_FAILURE_DETAIL_CHARS),
        stable_error=bounded_details[0] if bounded_details else bounded_failure_text(actual, MAX_FAILURE_DETAIL_CHARS),
        representative_excerpt=(
            bounded_details[0] if bounded_details else bounded_failure_text(actual, MAX_FAILURE_DETAIL_CHARS)
        ),
        affected_checks=(affected_check,),
        affected_count=1,
        artifact_refs=tuple(ref for ref in artifact_refs if ref is not None),
        retryable=True,
        retry_reason="The rejected generated candidate can plausibly be corrected within the same approved edit unit.",
    )
    history = failures_by_unit.setdefault(contract_unit, [])
    history.append(failure)
    if sum(item.fingerprint == failure.fingerprint for item in history) >= 2:
        return _repeated_failure_reason(failure)
    distinct = len({item.fingerprint for item in history})
    if distinct >= max_failure_groups:
        return (
            "Stopped automatic regeneration because the configurable distinct-root-cause ceiling "
            f"was reached ({distinct}/{max_failure_groups}); review the retained failure ledger."
        )
    return None


def _repeated_failure_reason(failure: AttemptFailure) -> str:
    return (
        "Stopped generation after the same deterministic failure repeated twice "
        f"({failure.category}, fingerprint {failure.fingerprint}). "
        f"Last failure: {_latest_failure_text((failure,))}"
    )


def _latest_failure_text(feedback: Sequence[AttemptFailure | str]) -> str:
    if not feedback:
        return ""
    latest = feedback[-1]
    if isinstance(latest, str):
        return latest
    return " ".join((latest.summary, *latest.details)).strip()


def _write_failure_artifact(
    work_dir: Path,
    *,
    contract_unit: str,
    attempt: int,
    category: str,
    payload: Mapping[str, Any],
) -> Path:
    artifact = {
        "contract_unit": contract_unit,
        "attempt": attempt,
        "category": category,
        "evidence": redact_failure_value(dict(payload)),
    }
    content = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    unit_digest = hashlib.sha256(contract_unit.encode("utf-8")).hexdigest()[:12]
    path = work_dir / "failures" / f"{unit_digest}-{attempt:02d}-{digest[:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise AutoWorkflowError(f"Failure artifact identity collision: {path}")
        return path
    path.write_text(content, encoding="utf-8")
    return path


def _scan(provider_repo: Path, validation_root: Path, domain: str, profile) -> dict[str, Any]:
    report = scan_provider(
        ScanOptions(provider_repo=provider_repo, domains=[domain], validation_root=validation_root)
    )
    if profile is not None:
        report = enrich_report_with_profile(report, profile)
    return report.to_dict()


def _load_profile(project: ReadinessProject, project_path: Path) -> Any:
    if not project.assessment.profile:
        return None
    return load_solution_profile(project.resolve_path(project_path, project.assessment.profile))


def _changed_files(original: Path, scratch: Path) -> list[ChangedFile]:
    # Deletions are outside the allowed change-set schema; only additions and replacements are tracked.
    changed: list[ChangedFile] = []
    for candidate in sorted(scratch.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(scratch)
        origin = original / relative
        after = _file_sha256(candidate)
        if origin.is_file():
            before = _file_sha256(origin)
            if before == after:
                continue
            changed.append(ChangedFile(path=relative.as_posix(), before_sha256=before, after_sha256=after, creates_file=False))
        else:
            changed.append(ChangedFile(path=relative.as_posix(), before_sha256=None, after_sha256=after, creates_file=True))
    return changed


def _combined_patch(original: Path, scratch: Path, changed_files: Sequence[ChangedFile]) -> str:
    parts: list[str] = []
    for change in changed_files:
        origin = original / change.path
        before = origin.read_text(encoding="utf-8").splitlines(keepends=True) if origin.is_file() else []
        after = (scratch / change.path).read_text(encoding="utf-8").splitlines(keepends=True)
        parts.append(
            "".join(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile="/dev/null" if change.creates_file else f"a/{change.path}",
                    tofile=f"b/{change.path}",
                )
            )
        )
    return "".join(parts)


def _apply_to_provider(
    original: Path, scratch: Path, changed_files: Sequence[ChangedFile], backup_dir: Path
) -> None:
    """Apply one reviewed scratch diff as a rollback-capable transaction."""

    targets: list[tuple[ChangedFile, Path, Path, int]] = []
    backups: dict[Path, Path] = {}
    staged: dict[Path, Path] = {}
    applied: list[Path] = []

    for change in changed_files:
        target = original / change.path
        source = scratch / change.path
        if not target.parent.is_dir():
            raise AutoWorkflowError(f"Provider target parent does not exist: {target.parent}")
        current = _file_sha256(target) if target.is_file() else None
        if current != change.before_sha256:
            raise AutoWorkflowError(f"Provider target changed after review: {change.path}")
        mode = (
            stat.S_IMODE(target.stat().st_mode)
            if target.is_file()
            else stat.S_IMODE(source.stat().st_mode)
        )
        targets.append((change, target, source, mode))

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_root = Path(tempfile.mkdtemp(prefix="review-", dir=backup_dir))
    try:
        for change, target, source, mode in targets:
            if target.is_file():
                backup = backup_root / change.path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[target] = backup
            staged[target] = _stage_file(source, target, mode)

        for change, target, _source, _mode in targets:
            os.replace(staged[target], target)
            staged.pop(target)
            applied.append(target)
            if _file_sha256(target) != change.after_sha256:
                raise AutoWorkflowError(f"Applied target hash does not match reviewed content: {change.path}")
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(applied):
            try:
                backup = backups.get(target)
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    restore = _stage_file(backup, target, stat.S_IMODE(backup.stat().st_mode))
                    try:
                        os.replace(restore, target)
                    finally:
                        restore.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise AutoWorkflowError(
                "Reviewed patch application failed and rollback was incomplete: "
                f"{exc}; {'; '.join(rollback_errors)}"
            ) from exc
        if isinstance(exc, AutoWorkflowError):
            raise
        raise AutoWorkflowError(f"Reviewed patch application failed and was rolled back: {exc}") from exc
    finally:
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)


def _stage_file(source: Path, target: Path, mode: int) -> Path:
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
            staged = Path(handle.name)
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)
        return staged
    except Exception:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise


def _discard_review_artifacts(work_dir: Path) -> None:
    for name in (
        "auto-review.json",
        "auto-review.patch",
        "domain-audit.pre.json",
        "domain-audit.post.json",
    ):
        (work_dir / name).unlink(missing_ok=True)


def _write_review(work_dir: Path, review: AutoReview) -> None:
    review_path = work_dir / "auto-review.json"
    patch_path = work_dir / "auto-review.patch"
    review_path.write_text(
        json.dumps(review.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if review.patch:
        patch_path.write_text(review.patch, encoding="utf-8")
    else:
        patch_path.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
