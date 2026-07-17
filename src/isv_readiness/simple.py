"""High-level wrapper commands for ISVs: init, fill, test, status."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from isv_readiness.auto import AutoWorkflowError, run_auto
from isv_readiness.context import ContextError
from isv_readiness.decision import blocking_rows, validation_profile_issues
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.live import LiveRunError, run_live_domain
from isv_readiness.onboarding import OnboardingError, build_provider_onboarding_plan, execute_provider_onboarding
from isv_readiness.project import ProjectError, ReadinessProject, build_bootstrap_plan, execute_bootstrap, load_project
from isv_readiness.qualify import QualifyError, build_qualify_catalog
from isv_readiness.runs import JUNIT_FILENAME, LOG_FILENAME, RunRecordError, latest_run, new_run_dir, write_run_record
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import SolutionProfileError, canonicalize_domain, load_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError


class SimpleError(ValueError):
    """Raised by high-level wrapper commands."""


_GENERATOR_ALIASES = {
    "claude": "gapctl-claude-generator",
    "codex": "gapctl-codex-generator",
}

def find_project() -> Path:
    """Walk up from CWD to find isv-project.yaml."""
    for directory in [Path.cwd(), *Path.cwd().parents]:
        candidate = directory / "isv-project.yaml"
        if candidate.is_file():
            return candidate
    raise SimpleError(
        "No isv-project.yaml found in this directory or any parent.\n"
        "Run `gapctl init <provider-name> --workspace <dir> --domains ...` first."
    )


def _resolve_generator(name: str) -> str:
    return _GENERATOR_ALIASES.get(name, name)


def _work_dir(project_path: Path, domain: str) -> Path:
    return project_path.parent / ".gapctl" / "work" / domain


def _gaps_path(project_path: Path) -> Path:
    return project_path.parent / "gaps.json"


def _artifacts_dir(project_path: Path) -> Path:
    return project_path.parent / ".gapctl" / "runs"


# ── init ──────────────────────────────────────────────────────────────────────


def cmd_init(
    provider_name: str,
    *,
    workspace: Path,
    domains: list[str],
    api_url: str | None,
    auth_envs: list[str],
    api_spec: str | None,
    validation_ref: str = "main",
) -> int:
    workspace = workspace.expanduser().resolve()
    project_path = workspace / "isv-project.yaml"

    # Bootstrap owns checkout creation, validation, and commit pinning. Build
    # the plan first so invalid input cannot trigger a clone.
    print(f"[1/3] Creating a pinned project for provider '{provider_name}'...")
    try:
        plan = build_bootstrap_plan(
            workspace,
            provider_name=provider_name,
            domains=domains,
            validation_ref=validation_ref,
            api_base_url=api_url,
            auth_env=auth_envs,
            api_spec=api_spec,
        )
        project = execute_bootstrap(plan, overwrite=False)
    except (OSError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolved_root = project.validation_root(project_path)
    print(f"  Created: {project_path}")
    print(f"  Pinned validation commit: {project.validation.resolved_commit}")

    # 2. Catalog: distill suites into .gapctl/catalog.json
    print("[2/3] Building check catalog...")
    try:
        adapter = IsvctlAdapter(project.validation_root(project_path))
        catalog = build_qualify_catalog(adapter, project.assessment.domains)
        catalog_out = project_path.parent / ".gapctl" / "catalog.json"
        catalog_out.parent.mkdir(parents=True, exist_ok=True)
        catalog_out.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checks = sum(len(e["checks"]) for e in catalog["domains"].values())
        print(f"  {checks} checks across {len(domains)} domain(s): {', '.join(domains)}")
    except (OSError, ProjectError, ValidationAdapterError, QualifyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 3. Onboard: scaffold provider script stubs for every domain
    print(f"[3/3] Scaffolding provider scripts for {', '.join(domains)}...")
    try:
        onboard_plan = build_provider_onboarding_plan(resolved_root, provider_name, domains)
        written = execute_provider_onboarding(onboard_plan, overwrite=False)
        for path in written:
            print(f"  Created: {path}")
    except (OSError, OnboardingError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print()
    print("Workspace created. Complete qualification before validation:")
    print("  gapctl qualify-draft --project isv-project.yaml --generator <generator>")
    print("  gapctl profile --in solution-profile.yaml --draft solution-profile.draft.yaml")
    print("After SME ratification, use gapctl fill, gapctl test, and gapctl status.")
    return 0


# ── fill ──────────────────────────────────────────────────────────────────────


def cmd_fill(
    domain: str,
    *,
    generator: str,
    approve_patch: str | None,
    max_iterations: int,
) -> int:
    try:
        project_path = find_project()
    except SimpleError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    generator_exe = _resolve_generator(generator)
    work_dir = _work_dir(project_path, domain)
    work_dir.mkdir(parents=True, exist_ok=True)

    applying = approve_patch is not None
    if applying:
        print(f"Applying approved patch {approve_patch[:12]}... for domain '{domain}'")
    else:
        print(f"Filling gaps for domain '{domain}' (generator: {generator_exe})...")

    try:
        review = run_auto(
            project_path,
            domain=domain,
            work_dir=work_dir,
            generator_command=[generator_exe],
            generator_pass_env=[],
            max_iterations=max_iterations,
            apply=applying,
            approval_patch_sha256=approve_patch,
        )
    except (OSError, AutoWorkflowError, ContextError, ProjectError, FixGuardrailError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"\nStatus: {review.status}")
    print(f"Staged: {len(review.staged)} gap(s)")
    for fix in review.staged:
        print(f"  + {fix.target or fix.gap_id}")

    if review.parked:
        print(f"Parked: {len(review.parked)} gap(s) (need manual attention)")
        for gap in review.parked:
            print(f"  ! {gap.gap_id}: {gap.reason}")

    if review.changed_files and not applying:
        print()
        print("Review the proposed changes, then approve:")
        print(f"  gapctl fill {domain} --generator {generator} --approve {review.patch_sha256}")

    return 0


# ── test ──────────────────────────────────────────────────────────────────────


def cmd_test(domain: str) -> int:
    try:
        project_path = find_project()
        project = load_project(project_path)
        canonical_domain = canonicalize_domain(domain)
    except (OSError, SimpleError, ProjectError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    gaps_out = _gaps_path(project_path)

    print(f"Running validation for domain '{canonical_domain}' against real cloud...")
    try:
        run_id, run_dir = new_run_dir(_artifacts_dir(project_path), canonical_domain)
        result = run_live_domain(
            project,
            project_path,
            domain=canonical_domain,
            artifacts_dir=run_dir,
            explicit_authorization=True,
        )
        _canonicalize_run_artifacts(result.junit_path, result.log_path, run_dir)
        write_run_record(
            run_dir,
            run_id=run_id,
            domain=result.domain,
            config=result.config,
            exit_code=result.exit_code,
        )
    except (OSError, ProjectError, LiveRunError, RunRecordError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"  Passed: {'yes' if result.success else 'no'}")
    print(f"  Artifacts: {run_dir}")

    print("\nUpdating gaps.json...")
    try:
        report = _scan_project_report(project, project_path)
        current_rows = result.report.get("rows")
        if not isinstance(current_rows, list):
            raise SimpleError("Live run returned an invalid gap report.")
        report["rows"] = sorted(
            [row for row in report["rows"] if row.get("domain") != result.domain] + current_rows,
            key=_row_sort_key,
        )
        gaps_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    open_gaps = len(blocking_rows(report, result.domain))
    print(f"  Open gaps in '{result.domain}': {open_gaps}")
    print()
    print(render_report(report, "scorecard"))
    return 0 if result.success else 1


# ── status ────────────────────────────────────────────────────────────────────


def cmd_status() -> int:
    try:
        project_path = find_project()
        project = load_project(project_path)
    except (OSError, SimpleError, ProjectError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    gaps_out = _gaps_path(project_path)
    try:
        report = load_report(gaps_out) if gaps_out.exists() else None
        expected_domains = set(project.assessment.domains)
        reported_domains = set(report.get("domains", [])) if isinstance(report, dict) else set()
        if report is None or reported_domains != expected_domains:
            reason = "No gaps.json found" if report is None else "gaps.json does not cover the full project scope"
            print(f"{reason} — running a full static scan...")
            report = _scan_project_report(project, project_path)
            gaps_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        rows = report.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SimpleError(f"Invalid rows in gap report: {gaps_out}")
    except (OSError, json.JSONDecodeError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        profile = (
            load_solution_profile(project.resolve_path(project_path, project.assessment.profile))
            if project.assessment.profile
            else None
        )
    except (OSError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(render_report(report, "scorecard"))
    open_gaps = len(blocking_rows(report))
    profile_issues = validation_profile_issues(profile, project.assessment.domains)
    passing_dynamic_domains = {
        row.get("domain")
        for row in rows
        if row.get("detection") == "dynamic" and row.get("status") == "pass"
    }
    unvalidated = [
        domain
        for domain in project.assessment.domains
        if domain not in passing_dynamic_domains
        or (record := latest_run(_artifacts_dir(project_path), domain)) is None
        or record.exit_code != 0
    ]
    if open_gaps == 0 and not unvalidated and not profile_issues:
        print("\nAll gaps are closed and every owned domain has a passing recorded live run.")
        print("Build the bundle from each completed agent-run work directory:")
        print(
            "  gapctl bundle --project isv-project.yaml "
            "--agent-work-dir <completed-agent-work-dir> --out-dir validation-bundle/"
        )
    else:
        if open_gaps:
            print(f"\n{open_gaps} gap(s) remaining.")
        if unvalidated:
            print("Live validation still required for: " + ", ".join(unvalidated))
        if profile_issues:
            print("Profile qualification still required: " + "; ".join(profile_issues))
    return 0 if open_gaps == 0 and not unvalidated and not profile_issues else 1


# ── helpers ───────────────────────────────────────────────────────────────────


def _scan_project_report(project: ReadinessProject, project_path: Path) -> dict:
    report = scan_provider(
        ScanOptions(
            provider_repo=project.provider_root(project_path),
            domains=list(project.assessment.domains),
            validation_root=project.validation_root(project_path),
        )
    )
    if project.assessment.profile:
        profile_path = project.resolve_path(project_path, project.assessment.profile)
        if profile_path.exists():
            report = enrich_report_with_profile(report, load_solution_profile(profile_path))
    return report.to_dict()


def _canonicalize_run_artifacts(junit_path: str | None, log_path: str, run_dir: Path) -> None:
    for raw_path, filename in ((junit_path, JUNIT_FILENAME), (log_path, LOG_FILENAME)):
        if raw_path is None:
            continue
        source = Path(raw_path)
        target = run_dir / filename
        if source.is_file() and source != target:
            source.replace(target)


def _row_sort_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("domain") or ""),
        str(row.get("step_name") or ""),
        str(row.get("validation_class") or ""),
        str(row.get("detection") or ""),
        str(row.get("id") or ""),
    )
