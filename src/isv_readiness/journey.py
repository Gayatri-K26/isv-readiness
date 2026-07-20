"""The complete ISV-facing journey: init, qualify, validate, publish.

The lower-level scanners, generators, guardrails, and live runners remain
implementation services.  They are deliberately not separate CLI workflows.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import yaml

from isv_readiness.auto import AutoReview, AutoWorkflowError, run_auto
from isv_readiness.context import (
    ContextError,
    build_qualify_pack,
    context_cache_is_current,
    load_context_records,
    sync_context_sources,
)
from isv_readiness.decision import validation_profile_issues
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.project import ProjectError, load_project
from isv_readiness.qualify import (
    QualifyError,
    build_qualify_catalog,
    empirical_conflicts,
    profile_draft_diff,
    run_profile_draft,
)
from isv_readiness.simple import SimpleError, cmd_status, find_project, run_test_domain
from isv_readiness.solution_profile import SolutionProfileError, load_solution_profile, parse_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError

Confirm = Callable[[str], bool]

_GENERATOR_ALIASES = {
    "claude": "gapctl-claude-generator",
    "codex": "gapctl-codex-generator",
}


def cmd_qualify(*, generator: str = "codex", confirm: Confirm | None = None) -> int:
    """Draft and ratify the declared scope through one resumable command."""

    try:
        project_path = find_project()
        project = load_project(project_path)
        active_path = project.resolve_path(project_path, project.assessment.profile or "solution-profile.yaml")
        active = load_solution_profile(active_path)
    except (OSError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not validation_profile_issues(active, project.assessment.domains):
        print("Qualification is already complete for: " + ", ".join(project.assessment.domains))
        return 0

    qualification_dir = project_path.parent / ".gapctl" / "qualification"
    proposal_path = qualification_dir / "solution-profile.proposed.yaml"
    if not proposal_path.exists():
        print("Building an evidence-grounded qualification proposal...")
        try:
            qualification_dir.mkdir(parents=True, exist_ok=True)
            cache_dir = project_path.parent / ".gapctl" / "context-cache"
            cached_records = load_context_records(cache_dir)
            if not context_cache_is_current(project, cache_dir) or any(
                record.status == "error" for record in cached_records
            ):
                records = sync_context_sources(project, project_path, cache_dir)
                failures = [record for record in records if record.status == "error"]
                if failures:
                    details = "; ".join(f"{record.source_id}: {record.error}" for record in failures)
                    raise ContextError(f"Required qualification context could not be imported: {details}")
            catalog = build_qualify_catalog(
                IsvctlAdapter(project.validation_root(project_path)), project.assessment.domains
            )
            pack = build_qualify_pack(project, catalog, cache_dir=cache_dir)
            raw = run_profile_draft(
                pack,
                command=[_resolve_generator(generator)],
                cwd=project_path.parent,
            )
            proposal_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        except (
            OSError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            ProjectError,
            ContextError,
            FixGuardrailError,
            QualifyError,
            SolutionProfileError,
            ValidationAdapterError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Proposal written: {proposal_path}")

    try:
        proposal = load_solution_profile(proposal_path)
        conflicts = empirical_conflicts(proposal, project_path.parent / ".gapctl" / "runs")
        if conflicts:
            for conflict in conflicts:
                print(f"Evidence conflict: {conflict}", file=sys.stderr)
            print(f"Resolve the conflicts in {proposal_path}, then run `gapctl qualify` again.", file=sys.stderr)
            return 1
        promoted_raw = _promoted_profile(proposal_path)
        promoted = parse_solution_profile(promoted_raw)
        issues = validation_profile_issues(promoted, project.assessment.domains)
    except (OSError, TypeError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("\nProposed scope changes:")
    for line in profile_draft_diff(active, proposal) or ["no domain differences"]:
        print(f"  {line}")
    if issues:
        print("\nThe proposal still has unresolved qualification decisions:")
        for issue in issues:
            print(f"  - {issue}")
        print(f"Edit {proposal_path}, then run `gapctl qualify` again.")
        return 1

    before_hash = _file_sha256(proposal_path)
    print(f"\nReview file: {proposal_path}")
    print(f"Profile hash: {before_hash}")
    if not (confirm or _confirm)("Approve this scope and enter validation?"):
        print("Qualification was not approved. Edit the proposal and run `gapctl qualify` again.")
        return 1
    if _file_sha256(proposal_path) != before_hash:
        print("The proposal changed during approval; review it and run `gapctl qualify` again.", file=sys.stderr)
        return 1

    backup = qualification_dir / "solution-profile.initial.yaml"
    if active_path.is_file() and not backup.exists():
        shutil.copy2(active_path, backup)
    active_path.write_text(yaml.safe_dump(promoted_raw, sort_keys=False), encoding="utf-8")
    print(f"Qualification approved: {active_path}")
    print("Next: gapctl validate")
    return 0


def cmd_validate(*, generator: str = "codex", confirm: Confirm | None = None) -> int:
    """Close static gaps, obtain review, and run every owned domain."""

    try:
        project_path = find_project()
        project = load_project(project_path)
        profile = (
            load_solution_profile(project.resolve_path(project_path, project.assessment.profile))
            if project.assessment.profile
            else None
        )
        issues = validation_profile_issues(profile, project.assessment.domains)
    except (OSError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if issues:
        print("Qualification is incomplete:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print("Run `gapctl qualify` first.", file=sys.stderr)
        return 1

    ask = confirm or _confirm
    generator_command = [_resolve_generator(generator)]
    for domain in project.assessment.domains:
        work_dir = project_path.parent / ".gapctl" / "work" / domain
        while True:
            try:
                review = _pending_review(work_dir, domain) or run_auto(
                    project_path,
                    domain=domain,
                    work_dir=work_dir,
                    generator_command=generator_command,
                    max_iterations=50,
                    apply=False,
                )
            except (
                OSError,
                subprocess.SubprocessError,
                AutoWorkflowError,
                ContextError,
                ProjectError,
                FixGuardrailError,
                SolutionProfileError,
            ) as exc:
                print(str(exc), file=sys.stderr)
                return 2

            if review.status == "awaiting_review":
                patch_path = work_dir / "auto-review.patch"
                print(f"\nStatically verified {domain} candidate patch: {patch_path}")
                print(f"Patch hash: {review.patch_sha256}")
                if not ask(f"Apply this reviewed {domain} patch?"):
                    print("Patch was not applied. Review it and run `gapctl validate` again.")
                    return 1
                try:
                    applied = run_auto(
                        project_path,
                        domain=domain,
                        work_dir=work_dir,
                        generator_command=generator_command,
                        max_iterations=50,
                        apply=True,
                        approval_patch_sha256=review.patch_sha256,
                    )
                except (
                    OSError,
                    subprocess.SubprocessError,
                    AutoWorkflowError,
                    ContextError,
                    ProjectError,
                    FixGuardrailError,
                    SolutionProfileError,
                ) as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
                if applied.status != "applied":
                    print(applied.reason, file=sys.stderr)
                    return 1
                print(f"Applied {len(applied.changed_files)} reviewed file(s) for {domain}.")
                # Rescan after application. A later gap may depend on an earlier
                # generated fix, and no live resources should be created while a
                # known static blocker remains.
                continue

            if review.parked:
                print(f"\n{domain} still has gaps requiring an SME or manual implementation:")
                for gap in review.parked:
                    print(f"  - {gap.gap_id}: {gap.reason}")
                return 1
            break

    if not ask("Run NVIDIA validation against the real cloud for every owned domain?"):
        print("Live validation was not started. Run `gapctl validate` when the environment is ready.")
        return 1

    # The explicit command plus the confirmation above is the public live-run
    # authorization. Keep the internal runner's policy gate intact by passing a
    # transient authorized project; no YAML toggle is required from the ISV.
    live_project = replace(project, execution=replace(project.execution, allow_live_runs=True))
    had_failure = False
    for domain in project.assessment.domains:
        result = run_test_domain(live_project, project_path, domain)
        if result == 2:
            return 2
        had_failure = had_failure or result != 0

    status = cmd_status(project_path=project_path)
    if status == 2:
        return 2
    return 1 if had_failure else status


def _pending_review(work_dir: Path, domain: str) -> AutoReview | None:
    review_path = work_dir / "auto-review.json"
    scratch = work_dir / "scratch-provider"
    patch_path = work_dir / "auto-review.patch"
    if not (review_path.is_file() and scratch.is_dir() and patch_path.is_file()):
        return None
    try:
        raw = json.loads(review_path.read_text(encoding="utf-8"))
        if raw.get("status") != "awaiting_review":
            return None
        patch = patch_path.read_text(encoding="utf-8")
        expected_hash = str(raw["patch_sha256"])
        if (
            raw.get("domain") != domain
            or raw.get("patch") != patch
            or hashlib.sha256(patch.encode()).hexdigest() != expected_hash
        ):
            raise AutoWorkflowError(
                f"Stored {domain} review state does not match the patch file; remove {work_dir} and run `gapctl validate` again."
            )
        return AutoReview(
            schema_version=str(raw["schema_version"]),
            domain=str(raw["domain"]),
            status=str(raw["status"]),
            patch=patch,
            patch_sha256=expected_hash,
            staged=(),
            parked=(),
            changed_files=(),
            reason=str(raw["reason"]),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _promoted_profile(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("solution"), dict)
        or not isinstance(raw.get("journey"), dict)
    ):
        raise SolutionProfileError(f"Qualification proposal has an invalid document shape: {path}")
    raw["solution"]["profile_status"] = "reviewed"
    raw["journey"]["stage"] = "validate"
    raw["journey"]["status"] = "in_progress"
    return raw


def _resolve_generator(name: str) -> str:
    executable = _GENERATOR_ALIASES.get(name)
    if executable is None:
        return name
    sibling = Path(sys.executable).parent / executable
    return str(sibling) if sibling.is_file() else executable


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False
