from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from isv_readiness.change_verification import apply_verified_change_set, verify_change_set
from isv_readiness.context import build_context_pack
from isv_readiness.generation import GeneratorRunner, run_generator
from isv_readiness.loop import FIX_ACTION, UNRESOLVED_STATUSES
from isv_readiness.project import ReadinessProject, load_project
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
        raise AutoWorkflowError("Provider is not scaffolded; run gapctl onboard --write first.")
    if not generator_command:
        raise AutoWorkflowError("An explicit generator adapter is required (--generator).")

    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = work_dir / "scratch-provider"
    scratch_backups = work_dir / "scratch-backups"
    for stale in (scratch, scratch_backups):
        if stale.exists():
            shutil.rmtree(stale)
    shutil.copytree(provider_root, scratch)

    profile = _load_profile(project, project_path)
    staged: list[StagedFix] = []
    attempts_by_gap: dict[str, int] = {}
    max_attempts = project.execution.max_attempts

    for _ in range(max_iterations):
        report = _scan(scratch, project.validation_root(project_path), canonical_domain, profile)
        fixable = _select_fixable(report, canonical_domain)
        # Drop gaps already staged (a re-scan should show them resolved; if it
        # does not, the retry budget below stops an infinite loop).
        pending = [row for row in fixable if row["id"] not in {fix.gap_id for fix in staged}]
        if not pending:
            break
        row = pending[0]
        gap_id = row["id"]
        if attempts_by_gap.get(gap_id, 0) >= max_attempts:
            break

        change_set = _generate(
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
            feedback=_feedback_for(attempts_by_gap.get(gap_id, 0)),
        )
        manifest = verify_change_set(
            report,
            provider_repo=scratch,
            change_set=change_set,
            validation_root=project.validation_root(project_path),
        )
        attempts_by_gap[gap_id] = attempts_by_gap.get(gap_id, 0) + 1
        if not manifest.success:
            # Leave the gap for the next iteration's retry budget; a fresh scan
            # keeps selecting it until the budget is exhausted, then it parks.
            continue
        apply_verified_change_set(
            report,
            provider_repo=scratch,
            change_set=change_set,
            manifest=manifest,
            backup_dir=scratch_backups,
        )
        staged.append(
            StagedFix(
                gap_id=gap_id,
                target=str(row.get("remediation", {}).get("target") or ""),
                validation_class=row.get("validation_class"),
                summary=change_set.summary,
                attempts=attempts_by_gap[gap_id],
            )
        )

    final_report = _scan(scratch, project.validation_root(project_path), canonical_domain, profile)
    parked = _park(final_report, canonical_domain, staged)
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

    status = "awaiting_review"
    reason = (
        f"{len(staged)} verified fix(es) staged for one review; {len(parked)} gap(s) parked. "
        "Review the combined patch and re-run with --apply <patch-sha256>."
    )
    if apply:
        if approval_patch_sha256 != patch_sha256:
            reason = (
                "Apply refused: supplied approval hash does not match the current combined patch. "
                "Review the patch and pass the exact printed --apply hash."
            )
        else:
            _apply_to_provider(provider_root, scratch, changed_files, work_dir / "backups")
            status = "applied"
            reason = f"Applied {len(changed_files)} file(s) atomically after hash-bound review approval."

    review = AutoReview(
        schema_version=AUTO_REVIEW_VERSION,
        domain=canonical_domain,
        status=status,
        patch=patch,
        patch_sha256=patch_sha256,
        staged=tuple(staged),
        parked=tuple(parked),
        changed_files=tuple(changed_files),
        reason=reason,
    )
    _write_review(work_dir, review)
    return review


def _select_fixable(report: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in report.get("rows", [])
        if row.get("domain") == domain
        and row.get("status") in UNRESOLVED_STATUSES
        and (row.get("remediation") or {}).get("auto_fixable") is True
        and _action(row) == FIX_ACTION
    ]
    rows.sort(key=lambda row: (str(row.get("step_name")), str(row.get("validation_class") or ""), str(row.get("id"))))
    return rows


def _park(report: dict[str, Any], domain: str, staged: Sequence[StagedFix]) -> list[ParkedGap]:
    staged_ids = {fix.gap_id for fix in staged}
    parked: list[ParkedGap] = []
    for row in report.get("rows", []):
        if row.get("domain") != domain or row.get("status") not in UNRESOLVED_STATUSES:
            continue
        if row.get("id") in staged_ids:
            continue
        if (row.get("remediation") or {}).get("auto_fixable") is True and _action(row) == FIX_ACTION:
            reason = "Auto-fix attempts were exhausted without a verified candidate."
        else:
            reason = f"Requires human route '{_action(row)}' before code can be generated."
        sp = (row.get("enrichment") or {}).get("solution_profile") or {}
        reconciliation = sp.get("reconciliation") or {}
        parked.append(
            ParkedGap(
                gap_id=str(row.get("id")),
                status=str(row.get("status")),
                action=_action(row),
                reason=reason,
                masked_failure=bool(reconciliation.get("masked_failure")),
            )
        )
    parked.sort(key=lambda item: item.gap_id)
    return parked


def _action(row: Mapping[str, Any]) -> str:
    sp = (row.get("enrichment") or {}).get("solution_profile") or {}
    action = sp.get("action")
    # Without a profile there is no routing; an auto-fixable row is treated as
    # implement_or_fix so the owned-domain scope filter still governs it.
    return str(action) if action else FIX_ACTION


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
    feedback: Sequence[str],
):
    context_pack = build_context_pack(
        project,
        project_path,
        report,
        gap_id=gap_id,
        cache_dir=project_path.parent / ".gapctl" / "context-cache",
        environment=environment,
        feedback=feedback,
    )
    return run_generator(
        context_pack.to_dict(),
        command=list(generator_command),
        cwd=work_dir,
        pass_env=generator_pass_env,
        runner=generator_runner,
        environment=environment,
    )


def _feedback_for(prior_attempts: int) -> tuple[str, ...]:
    if prior_attempts == 0:
        return ()
    return (f"Previous {prior_attempts} candidate(s) failed isolated verification; revise the approach.",)


def _scan(provider_repo: Path, validation_root: Path, domain: str, profile) -> dict[str, Any]:
    report = scan_provider(
        ScanOptions(provider_repo=provider_repo, domains=[domain], validation_root=validation_root)
    )
    if profile is not None:
        report = enrich_report_with_profile(report, profile)
    return report.to_dict()


def _load_profile(project: ReadinessProject, project_path: Path):
    if not project.assessment.profile:
        return None
    return load_solution_profile(project.resolve_path(project_path, project.assessment.profile))


def _changed_files(original: Path, scratch: Path) -> list[ChangedFile]:
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
    backup_dir.mkdir(parents=True, exist_ok=True)
    for change in changed_files:
        target = original / change.path
        source = scratch / change.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            backup = backup_dir / f"{change.path.replace('/', '_')}.bak"
            shutil.copy2(target, backup)
        tmp = target.with_suffix(target.suffix + ".gapctl-tmp")
        tmp.write_bytes(source.read_bytes())
        tmp.replace(target)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_review(work_dir: Path, review: AutoReview) -> None:
    (work_dir / "auto-review.json").write_text(
        json.dumps(review.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if review.patch:
        (work_dir / "auto-review.patch").write_text(review.patch, encoding="utf-8")
