"""Shared implementations used by the four-command ISV journey."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from isv_readiness.context import ContextError, sync_context_sources
from isv_readiness.decision import blocking_rows
from isv_readiness.live import LiveRunError, run_live_domain
from isv_readiness.onboarding import OnboardingError, build_provider_onboarding_plan, execute_provider_onboarding
from isv_readiness.project import ProjectError, ReadinessProject, build_bootstrap_plan, execute_bootstrap, load_project
from isv_readiness.qualify import QualifyError, build_qualify_catalog
from isv_readiness.readiness import assess_readiness
from isv_readiness.runs import JUNIT_FILENAME, LOG_FILENAME, RunRecordError, new_run_dir, write_run_record
from isv_readiness.scan.models import SCHEMA_VERSION
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import SolutionProfileError, canonicalize_domain, load_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError


class SimpleError(ValueError):
    """Raised by high-level wrapper commands."""


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
    input_envs: list[str] | None = None,
    api_spec: str | None,
    validation_ref: str = "main",
    validation_root: Path | None = None,
) -> int:
    workspace = workspace.expanduser().resolve()
    project_path = workspace / "isv-project.yaml"
    resolved_api_spec = api_spec
    if api_spec and not api_spec.startswith(("https://", "http://")):
        spec_path = Path(api_spec).expanduser()
        spec_path = spec_path.resolve() if spec_path.is_absolute() else (Path.cwd() / spec_path).resolve()
        if not spec_path.is_file():
            print(f"API specification file not found: {spec_path}", file=sys.stderr)
            return 2
        resolved_api_spec = str(spec_path)

    # Bootstrap owns checkout creation, validation, and commit pinning. Build
    # the plan first so invalid input cannot trigger a clone.
    print(f"[1/4] Creating a pinned project for provider '{provider_name}'...")
    try:
        plan = build_bootstrap_plan(
            workspace,
            provider_name=provider_name,
            domains=domains,
            validation_ref=validation_ref,
            validation_root=validation_root,
            api_base_url=api_url,
            api_base_url_env="ISV_API_BASE_URL" if api_url else None,
            auth_env=auth_envs,
            pass_env=input_envs or [],
            api_spec=resolved_api_spec,
        )
        project = execute_bootstrap(plan, overwrite=False)
    except (OSError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolved_root = project.validation_root(project_path)
    print(f"  Created: {project_path}")
    print(f"  Pinned validation commit: {project.validation.resolved_commit}")

    # 2. Catalog: distill suites into .gapctl/catalog.json
    print("[2/4] Building check catalog...")
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

    # 3. Onboard only when the checkout does not already contain this provider.
    # Existing implementations are input to qualification and must not be
    # overwritten or partially supplemented during initialization.
    if project.provider.state == "existing":
        print(f"[3/4] Using existing provider implementation: {project.provider_root(project_path)}")
    else:
        print(f"[3/4] Scaffolding provider scripts for {', '.join(domains)}...")
        try:
            onboard_plan = build_provider_onboarding_plan(resolved_root, provider_name, domains)
            written = execute_provider_onboarding(onboard_plan, overwrite=False)
            for path in written:
                print(f"  Created: {path}")
        except (OSError, OnboardingError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

    # Context belongs to initialization, not to a separate operator workflow.
    # Optional NVIDIA sources may be unavailable; a declared API specification
    # is required and therefore fails initialization if it cannot be imported.
    print("[4/4] Importing qualification context...")
    try:
        records = sync_context_sources(project, project_path, project_path.parent / ".gapctl" / "context-cache")
    except (OSError, ContextError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    required_failures = [record for record in records if record.status == "error"]
    for record in records:
        print(f"  {record.source_id}: {record.status}")
    if required_failures:
        for record in required_failures:
            print(f"Required context failed: {record.source_id}: {record.error}", file=sys.stderr)
        return 2

    print()
    print("Workspace created.")
    print(f"  cd {workspace}")
    print("  gapctl qualify")
    return 0


# ── live evidence ─────────────────────────────────────────────────────────────


def cmd_test(domain: str) -> int:
    try:
        project_path = find_project()
        project = load_project(project_path)
    except (OSError, SimpleError, ProjectError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return run_test_domain(project, project_path, domain)


def run_test_domain(project: ReadinessProject, project_path: Path, domain: str) -> int:
    """Run and record one domain while preserving other domains' evidence."""

    try:
        canonical_domain = canonicalize_domain(domain)
    except SolutionProfileError as exc:
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
        existing_dynamic: list[dict] = []
        if gaps_out.is_file():
            existing = load_report(gaps_out)
            if existing.get("schema_version") == SCHEMA_VERSION and set(existing.get("domains", [])) == set(
                project.assessment.domains
            ):
                existing_dynamic = [
                    row
                    for row in existing.get("rows", [])
                    if isinstance(row, dict)
                    and row.get("domain") != result.domain
                    and row.get("detection") == "dynamic"
                ]
        current_rows = result.report.get("rows")
        if not isinstance(current_rows, list):
            raise SimpleError("Live run returned an invalid gap report.")
        report["rows"] = sorted(
            [row for row in report["rows"] if row.get("domain") != result.domain] + existing_dynamic + current_rows,
            key=_row_sort_key,
        )
        gaps_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    open_gaps = len(blocking_rows(report, result.domain))
    print(f"  Open gaps in '{result.domain}': {open_gaps}")
    print()
    print(render_report(report, "scorecard"))
    return 0 if result.success else 1


# ── status ────────────────────────────────────────────────────────────────────


def cmd_status(*, project_path: Path | None = None) -> int:
    try:
        project_path = project_path or find_project()
        project = load_project(project_path)
    except (OSError, SimpleError, ProjectError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    gaps_out = _gaps_path(project_path)
    try:
        report = load_report(gaps_out) if gaps_out.exists() else None
        expected_domains = set(project.assessment.domains)
        reported_domains = set(report.get("domains", [])) if isinstance(report, dict) else set()
        reported_schema = report.get("schema_version") if isinstance(report, dict) else None
        if report is None or reported_domains != expected_domains or reported_schema != SCHEMA_VERSION:
            if report is None:
                reason = "No gaps.json found"
            elif reported_domains != expected_domains:
                reason = "gaps.json does not cover the full project scope"
            else:
                reason = f"gaps.json uses schema {reported_schema!r}; current schema is {SCHEMA_VERSION!r}"
            print(f"{reason} — running a full static scan...")
            report = _scan_project_report(project, project_path)
            gaps_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        rows = report.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SimpleError(f"Invalid rows in gap report: {gaps_out}")
    except (OSError, json.JSONDecodeError, ProjectError, SimpleError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(render_report(report, "scorecard"))
    try:
        readiness = assess_readiness(project, project_path, report)
    except (OSError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if readiness.ready:
        print("\nAll gaps are closed and every owned domain has a passing recorded live run.")
        print("Ready to publish: gapctl publish --lab-id <nvidia-assigned-lab-id>")
    else:
        if readiness.blocking_count:
            print(f"\n{readiness.blocking_count} gap(s) remaining.")
        if readiness.unvalidated_domains:
            print("Live validation still required for: " + ", ".join(readiness.unvalidated_domains))
        if readiness.profile_issues:
            print("Profile qualification still required: " + "; ".join(readiness.profile_issues))
        for issue in (*readiness.report_issues, *readiness.evidence_issues):
            print(f"Evidence issue: {issue}")
    return 0 if readiness.ready else 1


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
