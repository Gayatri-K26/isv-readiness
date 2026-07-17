from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.change_verification import (
    apply_verified_change_set,
    load_change_application,
    load_change_verification,
    rollback_change_application,
    verify_change_set,
)
from isv_readiness.changes import build_change_proposal, canonical_sha256, load_change_set
from isv_readiness.context import build_context_pack
from isv_readiness.fixes import select_gap
from isv_readiness.generation import GeneratorRunner, run_generator
from isv_readiness.live import CommitResolver, LiveRunner, run_live_domain
from isv_readiness.loop import advance_loop
from isv_readiness.onboarding import build_provider_onboarding_plan, execute_provider_onboarding
from isv_readiness.project import ReadinessProject, load_project
from isv_readiness.scan.models import GapReport
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import canonicalize_domain, load_solution_profile

AGENT_STATE_VERSION = "0.1.0"


class AgentWorkflowError(ValueError):
    """Raised when the agent workflow cannot advance without violating a gate."""


@dataclass(frozen=True)
class AgentHistory:
    status: str
    gap_id: str | None
    reason: str


@dataclass(frozen=True)
class AgentState:
    schema_version: str
    project_sha256: str
    domain: str
    status: str
    iteration: int
    attempts: int
    selected_gap_id: str | None
    patch_sha256: str | None
    reason: str
    artifacts: dict[str, str]
    feedback: tuple[str, ...]
    history: tuple[AgentHistory, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["feedback"] = list(self.feedback)
        payload["history"] = [asdict(item) for item in self.history]
        return payload


def run_agent_turn(
    project_path: Path,
    *,
    domain: str,
    work_dir: Path,
    generator_command: Sequence[str] | None = None,
    generator_pass_env: Sequence[str] = (),
    approval_patch_sha256: str | None = None,
    apply_changes: bool = False,
    run_live: bool = False,
    onboard_if_missing: bool = False,
    environment: Mapping[str, str] | None = None,
    generator_runner: GeneratorRunner | None = None,
    live_runner: LiveRunner | None = None,
    commit_resolver: CommitResolver | None = None,
) -> AgentState:
    project_path = project_path.expanduser().resolve()
    project = load_project(project_path)
    canonical_domain = canonicalize_domain(domain)
    if canonical_domain not in project.assessment.domains:
        raise AgentWorkflowError(f"Domain '{canonical_domain}' is outside the selected project scope.")
    provider_root = project.provider_root(project_path)
    if not provider_root.is_dir():
        if not onboard_if_missing:
            raise AgentWorkflowError(
                "Provider is not scaffolded; rerun with --onboard or run gapctl onboard --write first."
            )
        profile = (
            load_solution_profile(project.resolve_path(project_path, project.assessment.profile))
            if project.assessment.profile
            else None
        )
        plan = build_provider_onboarding_plan(
            project.validation_root(project_path),
            project.provider.name,
            project.assessment.domains,
            profile=profile,
        )
        execute_provider_onboarding(plan)
    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    state_path = work_dir / "agent-state.json"
    identity = project_identity(project, project_path)
    state = load_agent_state(state_path) if state_path.exists() else _initial_state(identity, canonical_domain)
    if state.project_sha256 != identity or state.domain != canonical_domain:
        raise AgentWorkflowError("Agent state belongs to a different project identity or domain.")
    if state.status in {"blocked", "complete"}:
        return state

    if state.status == "awaiting_review":
        if not apply_changes or approval_patch_sha256 != state.patch_sha256:
            return state
        state = _apply_reviewed_change(project, project_path, state, work_dir)
        _save_state(state_path, state)

    if state.status == "awaiting_live":
        if not run_live:
            return state
        state = _run_live_and_advance(
            project,
            project_path,
            state,
            work_dir,
            generator_command=generator_command,
            generator_pass_env=generator_pass_env,
            environment=environment,
            generator_runner=generator_runner,
            live_runner=live_runner,
            commit_resolver=commit_resolver,
        )
        _save_state(state_path, state)
        return state

    state = _prepare_next_change(
        project,
        project_path,
        state,
        work_dir,
        generator_command=generator_command,
        generator_pass_env=generator_pass_env,
        run_live=run_live,
        environment=environment,
        generator_runner=generator_runner,
        live_runner=live_runner,
        commit_resolver=commit_resolver,
    )
    _save_state(state_path, state)
    return state


def load_agent_state(path: Path) -> AgentState:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_state(raw)
    return AgentState(
        schema_version=raw["schema_version"],
        project_sha256=raw["project_sha256"],
        domain=raw["domain"],
        status=raw["status"],
        iteration=raw["iteration"],
        attempts=raw["attempts"],
        selected_gap_id=raw["selected_gap_id"],
        patch_sha256=raw["patch_sha256"],
        reason=raw["reason"],
        artifacts=dict(raw["artifacts"]),
        feedback=tuple(raw["feedback"]),
        history=tuple(AgentHistory(**item) for item in raw["history"]),
    )


def _prepare_next_change(
    project: ReadinessProject,
    project_path: Path,
    state: AgentState,
    work_dir: Path,
    *,
    generator_command: Sequence[str] | None,
    generator_pass_env: Sequence[str],
    run_live: bool,
    environment: Mapping[str, str] | None,
    generator_runner: GeneratorRunner | None,
    live_runner: LiveRunner | None,
    commit_resolver: CommitResolver | None,
) -> AgentState:
    report_path_value = state.artifacts.get("feedback_report")
    if report_path_value:
        report = load_report(Path(report_path_value))
    else:
        report = _scan_project(project, project_path, state.domain)
    report_path = work_dir / f"gaps-{state.iteration:03d}.json"
    _write_json(report_path, report)
    decision = advance_loop(
        report,
        domain=state.domain,
        previous=None,
        max_attempts=project.execution.max_attempts,
    )
    if decision.status == "blocked":
        return _transition(
            state,
            status="blocked",
            gap_id=decision.selected_gap_id,
            reason=decision.reason,
            artifacts={"report": str(report_path)},
        )
    if decision.status == "complete":
        awaiting = _transition(
            state,
            status="awaiting_live",
            gap_id=None,
            reason="Static selected scope is green; a full-domain live run is required before completion.",
            artifacts={"report": str(report_path), "selection": ""},
        )
        if not run_live:
            return awaiting
        return _run_live_and_advance(
            project,
            project_path,
            awaiting,
            work_dir,
            generator_command=generator_command,
            generator_pass_env=generator_pass_env,
            environment=environment,
            generator_runner=generator_runner,
            live_runner=live_runner,
            commit_resolver=commit_resolver,
        )
    gap_id = decision.selected_gap_id
    if gap_id is None:
        raise AgentWorkflowError("Loop returned ready without a selected gap.")

    context_pack = build_context_pack(
        project,
        project_path,
        report,
        gap_id=gap_id,
        cache_dir=project_path.parent / ".gapctl" / "context-cache",
        environment=environment,
        feedback=state.feedback,
    )
    context_path = work_dir / f"context-{state.iteration:03d}-{gap_id}.json"
    _write_json(context_path, context_pack.to_dict())
    if not generator_command:
        return _transition(
            state,
            status="awaiting_generator",
            gap_id=gap_id,
            reason="Context is ready; configure an explicit generator adapter.",
            artifacts={"report": str(report_path), "context": str(context_path)},
        )

    change_set = run_generator(
        context_pack.to_dict(),
        command=generator_command,
        cwd=work_dir,
        pass_env=generator_pass_env,
        runner=generator_runner,
        environment=environment,
    )
    changes_path = work_dir / f"changes-{state.iteration:03d}-{gap_id}.json"
    _write_json(changes_path, change_set.to_dict())
    proposal = build_change_proposal(
        report,
        provider_repo=project.provider_root(project_path),
        change_set=change_set,
    )
    proposal_path = work_dir / f"proposal-{state.iteration:03d}-{gap_id}.json"
    patch_path = work_dir / f"proposal-{state.iteration:03d}-{gap_id}.patch"
    _write_json(proposal_path, proposal.to_dict())
    patch_path.write_text(proposal.patch, encoding="utf-8")
    verification = verify_change_set(
        report,
        provider_repo=project.provider_root(project_path),
        change_set=change_set,
        validation_root=project.validation_root(project_path),
    )
    verification_path = work_dir / f"verification-{state.iteration:03d}-{gap_id}.json"
    _write_json(verification_path, verification.to_dict())
    artifacts = {
        "report": str(report_path),
        "context": str(context_path),
        "changes": str(changes_path),
        "proposal": str(proposal_path),
        "patch": str(patch_path),
        "verification": str(verification_path),
    }
    if not verification.success:
        attempts = state.attempts + 1
        reason = (
            f"Static verification failed ({verification.selected_status_after or 'selected row missing'}); "
            f"{len(verification.regressions)} regression(s)."
        )
        feedback = (
            reason,
            *(f"Regression: {item}" for item in verification.regressions),
        )
        if attempts >= project.execution.max_attempts:
            return _transition(
                state,
                status="blocked",
                gap_id=gap_id,
                reason=f"{reason} Retry budget exhausted.",
                artifacts=artifacts,
                attempts=attempts,
                feedback=feedback,
            )
        return _transition(
            state,
            status="ready",
            gap_id=gap_id,
            reason=f"{reason} A revised candidate may be generated.",
            artifacts=artifacts,
            attempts=attempts,
            feedback=feedback,
            iteration=state.iteration + 1,
        )
    return _transition(
        state,
        status="awaiting_review",
        gap_id=gap_id,
        reason="Generated change set passed isolated static verification; review the patch and approve its hash.",
        artifacts=artifacts,
        patch_sha256=proposal.patch_sha256,
    )


def _apply_reviewed_change(
    project: ReadinessProject,
    project_path: Path,
    state: AgentState,
    work_dir: Path,
) -> AgentState:
    report = load_report(Path(state.artifacts["report"]))
    change_set = load_change_set(Path(state.artifacts["changes"]))
    manifest = load_change_verification(Path(state.artifacts["verification"]))
    result = apply_verified_change_set(
        report,
        provider_repo=project.provider_root(project_path),
        change_set=change_set,
        manifest=manifest,
        backup_dir=work_dir / "backups",
    )
    application_path = work_dir / f"application-{state.iteration:03d}-{state.selected_gap_id}.json"
    _write_json(application_path, result.to_dict())
    selected = select_gap(report, state.selected_gap_id or "")
    selection = selected.get("validation_class")
    artifacts = {**state.artifacts, "application": str(application_path), "selection": selection or ""}
    return _transition(
        state,
        status="awaiting_live",
        gap_id=state.selected_gap_id,
        reason="Reviewed change set was applied; targeted live verification is required.",
        artifacts=artifacts,
    )


def _run_live_and_advance(
    project: ReadinessProject,
    project_path: Path,
    state: AgentState,
    work_dir: Path,
    *,
    generator_command: Sequence[str] | None,
    generator_pass_env: Sequence[str],
    environment: Mapping[str, str] | None,
    generator_runner: GeneratorRunner | None,
    live_runner: LiveRunner | None,
    commit_resolver: CommitResolver | None,
) -> AgentState:
    selection = state.artifacts.get("selection") or None
    live = run_live_domain(
        project,
        project_path,
        domain=state.domain,
        artifacts_dir=work_dir / f"live-{state.iteration:03d}",
        explicit_authorization=True,
        selection=selection,
        runner=live_runner,
        commit_resolver=commit_resolver,
        environment=environment,
    )
    live_path = work_dir / f"live-{state.iteration:03d}.json"
    live_report_path = work_dir / f"live-gaps-{state.iteration:03d}.json"
    _write_json(live_path, live.to_dict())
    _write_json(live_report_path, live.report)
    if live.success:
        if selection is None:
            return _transition(
                state,
                status="complete",
                gap_id=None,
                reason="Static selected scope and the full-domain live validation are green.",
                artifacts={**state.artifacts, "live": str(live_path), "live_report": str(live_report_path)},
                attempts=0,
                feedback=(),
                patch_sha256=None,
            )
        advanced = _transition(
            state,
            status="ready",
            gap_id=None,
            reason="Targeted live verification passed; scanning for the next selected-scope gap.",
            artifacts={"live": str(live_path), "live_report": str(live_report_path)},
            attempts=0,
            feedback=(),
            patch_sha256=None,
            iteration=state.iteration + 1,
        )
        return _prepare_next_change(
            project,
            project_path,
            advanced,
            work_dir,
            generator_command=generator_command,
            generator_pass_env=generator_pass_env,
            run_live=False,
            environment=environment,
            generator_runner=generator_runner,
            live_runner=live_runner,
            commit_resolver=commit_resolver,
        )

    rollback_path: Path | None = None
    if application_path := state.artifacts.get("application"):
        rollback = rollback_change_application(
            load_change_application(Path(application_path)),
            provider_repo=project.provider_root(project_path),
        )
        rollback_path = work_dir / f"rollback-{state.iteration:03d}-{state.selected_gap_id}.json"
        _write_json(rollback_path, rollback.to_dict())
    attempts = state.attempts + 1
    unresolved = [
        row
        for row in live.report.get("rows", [])
        if row.get("detection") == "dynamic" and row.get("status") in {"fail", "error", "not_implemented"}
    ]
    feedback = (
        f"Live verification failed with exit code {live.exit_code}; statuses: {', '.join(live.selected_statuses) or 'none'}.",
        *(str((row.get("evidence") or {}).get("message") or row.get("id")) for row in unresolved[:10]),
    )
    artifacts = {"live": str(live_path), "feedback_report": str(live_report_path)}
    if rollback_path:
        artifacts["rollback"] = str(rollback_path)
    if attempts >= project.execution.max_attempts:
        return _transition(
            state,
            status="blocked",
            gap_id=state.selected_gap_id,
            reason="Live verification failed and the retry budget is exhausted; the applied change was rolled back.",
            artifacts=artifacts,
            attempts=attempts,
            feedback=feedback,
            patch_sha256=None,
        )
    retry = _transition(
        state,
        status="ready",
        gap_id=None,
        reason="Live verification failed; the applied change was rolled back and feedback is ready for triage.",
        artifacts=artifacts,
        attempts=attempts,
        feedback=feedback,
        patch_sha256=None,
        iteration=state.iteration + 1,
    )
    return _prepare_next_change(
        project,
        project_path,
        retry,
        work_dir,
        generator_command=generator_command,
        generator_pass_env=generator_pass_env,
        run_live=False,
        environment=environment,
        generator_runner=generator_runner,
        live_runner=live_runner,
        commit_resolver=commit_resolver,
    )


def _scan_project(project: ReadinessProject, project_path: Path, domain: str) -> dict[str, Any]:
    report = scan_provider(
        ScanOptions(
            provider_repo=project.provider_root(project_path),
            domains=[domain],
            validation_root=project.validation_root(project_path),
        )
    )
    if project.assessment.profile:
        profile = load_solution_profile(project.resolve_path(project_path, project.assessment.profile))
        report = enrich_report_with_profile(report, profile)
    return report.to_dict()


def _initial_state(project_sha256: str, domain: str) -> AgentState:
    return AgentState(
        schema_version=AGENT_STATE_VERSION,
        project_sha256=project_sha256,
        domain=domain,
        status="ready",
        iteration=0,
        attempts=0,
        selected_gap_id=None,
        patch_sha256=None,
        reason="Agent workflow initialized.",
        artifacts={},
        feedback=(),
        history=(),
    )


def _transition(
    state: AgentState,
    *,
    status: str,
    gap_id: str | None,
    reason: str,
    artifacts: dict[str, str],
    attempts: int | None = None,
    feedback: Sequence[str] | None = None,
    patch_sha256: str | None = None,
    iteration: int | None = None,
) -> AgentState:
    history = (*state.history, AgentHistory(status=status, gap_id=gap_id, reason=reason))[-100:]
    return AgentState(
        schema_version=state.schema_version,
        project_sha256=state.project_sha256,
        domain=state.domain,
        status=status,
        iteration=state.iteration if iteration is None else iteration,
        attempts=state.attempts if attempts is None else attempts,
        selected_gap_id=gap_id,
        patch_sha256=patch_sha256,
        reason=reason,
        artifacts=artifacts,
        feedback=state.feedback if feedback is None else tuple(feedback),
        history=history,
    )


def project_identity(project: ReadinessProject, project_path: Path) -> str:
    raw = project.to_dict()
    profile_sha256 = None
    if project.assessment.profile:
        profile_path = project.resolve_path(project_path, project.assessment.profile)
        if profile_path.is_file():
            profile_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    return canonical_sha256(
        {
            "schema_version": raw["schema_version"],
            "validation": raw["validation"],
            "provider": raw["provider"],
            "assessment": raw["assessment"],
            "apis": raw["apis"],
            "context_sources": raw["context_sources"],
            "profile_sha256": profile_sha256,
        }
    )


def _save_state(path: Path, state: AgentState) -> None:
    _validate_state(state.to_dict())
    _write_json(path, state.to_dict())


def _validate_state(raw: Any) -> None:
    try:
        jsonschema.validate(raw, load_schema("agent-state.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "agent_state"
        raise AgentWorkflowError(f"Invalid agent state at {location}: {exc.message}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, GapReport):
        value = value.to_dict()
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
