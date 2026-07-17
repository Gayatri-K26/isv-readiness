"""High-level wrapper commands for ISVs: init, fill, test, status."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from isv_readiness.auto import AutoWorkflowError, run_auto
from isv_readiness.context import ContextError
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.live import LiveRunError, run_live_domain
from isv_readiness.onboarding import OnboardingError, build_provider_onboarding_plan, execute_provider_onboarding
from isv_readiness.project import ProjectError, build_bootstrap_plan, execute_bootstrap, load_project
from isv_readiness.qualify import QualifyError, build_qualify_catalog
from isv_readiness.scan.dynamic import DynamicArtifacts, scan_dynamic_artifacts
from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.models import GapReport
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import SolutionProfileError
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError


class SimpleError(ValueError):
    """Raised by high-level wrapper commands."""


_GENERATOR_ALIASES = {
    "claude": "gapctl-claude-generator",
    "codex": "gapctl-codex-generator",
}

_ACI_REPO_URL = "https://github.com/NVIDIA/ai-cloud-validation.git"


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


def _find_validation_root() -> Path | None:
    """Find ai-cloud-validation/ cloned by gapctl init, next to isv-project.yaml."""
    try:
        project_path = find_project()
        candidate = project_path.parent / "ai-cloud-validation"
        if candidate.is_dir() and (candidate / "pyproject.toml").exists():
            return candidate
    except SimpleError:
        pass
    return None


def _clone_validation_repo(workspace: Path, ref: str = "main") -> Path:
    """Clone ai-cloud-validation into workspace/ if not already present."""
    checkout = workspace / "ai-cloud-validation"
    if checkout.exists():
        print(f"  Found existing checkout: {checkout}")
        return checkout
    print("  Cloning NVIDIA/ai-cloud-validation (this may take a moment)...")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, _ACI_REPO_URL, str(checkout)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SimpleError(f"Failed to clone ai-cloud-validation:\n{result.stderr.strip()}")
    print(f"  Cloned to: {checkout}")
    return checkout


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
    workspace.mkdir(parents=True, exist_ok=True)
    project_path = workspace / "isv-project.yaml"

    # 1. Clone ai-cloud-validation into the workspace
    print("[1/4] Cloning ai-cloud-validation...")
    try:
        resolved_root = _clone_validation_repo(workspace, ref=validation_ref)
    except SimpleError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 2. Bootstrap: write isv-project.yaml
    print(f"[2/4] Bootstrapping project for provider '{provider_name}'...")
    try:
        plan = build_bootstrap_plan(
            workspace,
            provider_name=provider_name,
            domains=domains,
            validation_root=resolved_root,
            api_base_url=api_url,
            auth_env=auth_envs,
            api_spec=api_spec,
        )
        execute_bootstrap(plan, overwrite=False)
    except (OSError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"  Created: {project_path}")

    # 3. Catalog: distill suites into .gapctl/catalog.json
    print("[3/4] Building check catalog...")
    try:
        project = load_project(project_path)
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

    # 4. Onboard: scaffold provider script stubs for every domain
    print(f"[4/4] Scaffolding provider scripts for {', '.join(domains)}...")
    try:
        onboard_plan = build_provider_onboarding_plan(resolved_root, provider_name, domains)
        written = execute_provider_onboarding(onboard_plan, overwrite=False)
        for path in written:
            print(f"  Created: {path}")
    except (OSError, OnboardingError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print()
    print("Ready. Next steps:")
    print("  gapctl fill <domain> --generator <generator>   # fill gaps with AI")
    print("  gapctl test <domain>                           # run against real cloud")
    print("  gapctl status                                  # check progress")
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
    print(f"Fixed:  {len(review.staged)} gap(s)")
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
    except SimpleError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    project = load_project(project_path)
    artifacts_dir = _artifacts_dir(project_path)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    gaps_out = _gaps_path(project_path)

    print(f"Running validation for domain '{domain}' against real cloud...")
    try:
        result = run_live_domain(
            project,
            project_path,
            domain=domain,
            artifacts_dir=artifacts_dir,
            explicit_authorization=True,
        )
    except (OSError, ProjectError, LiveRunError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"  Passed: {'yes' if result.success else 'no'}")
    print(f"  Artifacts: {artifacts_dir}")

    junit_path: Path | None = None
    if result.junit_path and Path(result.junit_path).exists():
        junit_path = Path(result.junit_path)

    print("\nUpdating gaps.json...")
    try:
        report = scan_provider(
            ScanOptions(
                provider_repo=project.provider_root(project_path),
                domains=[domain],
                validation_root=project.validation_root(project_path),
            )
        )

        if junit_path is not None:
            config_path = _first_provider_config(report, project.provider_root(project_path))
            if domain in {"k8s", "kubernetes"}:
                dynamic_rows = scan_k8s_artifacts(
                    K8sDynamicArtifacts(
                        provider_repo=project.provider_root(project_path),
                        junit_path=junit_path,
                        log_path=None,
                        setup_json_path=None,
                        config_path=config_path,
                        scope=None,
                    )
                )
            else:
                dynamic_rows = scan_dynamic_artifacts(
                    DynamicArtifacts(
                        provider_repo=project.provider_root(project_path),
                        domain=domain,
                        junit_path=junit_path,
                        log_path=None,
                        config_path=config_path,
                        static_rows=tuple(report.rows),
                    )
                )
            report = GapReport(
                schema_version=report.schema_version,
                provider_repo=report.provider_repo,
                domains=report.domains,
                rows=sorted(
                    [*report.rows, *dynamic_rows],
                    key=lambda r: (r.domain, r.step_name, r.validation_class or "", r.detection, r.id),
                ),
            )

        if project.assessment.profile:
            from isv_readiness.solution_profile import load_solution_profile
            profile_path = project.resolve_path(project_path, project.assessment.profile)
            if profile_path.exists():
                profile = load_solution_profile(profile_path)
                report = enrich_report_with_profile(report, profile)

        gaps_out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    open_gaps = sum(1 for r in report.rows if r.status != "pass")
    print(f"  Open gaps in '{domain}': {open_gaps}")
    print()
    print(render_report(report, "scorecard"))
    return 0 if result.success else 1


# ── status ────────────────────────────────────────────────────────────────────


def cmd_status() -> int:
    try:
        project_path = find_project()
    except SimpleError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    project = load_project(project_path)
    gaps_out = _gaps_path(project_path)

    if gaps_out.exists():
        report = load_report(gaps_out)
    else:
        print("No gaps.json found — running static scan...")
        report = scan_provider(
            ScanOptions(
                provider_repo=project.provider_root(project_path),
                domains=list(project.assessment.domains),
                validation_root=project.validation_root(project_path),
            )
        )
        gaps_out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(render_report(report, "scorecard"))
    open_gaps = sum(1 for r in report.rows if r.status != "pass")
    if open_gaps == 0:
        print("\nAll gaps closed. Ready to bundle:")
        print("  gapctl bundle --project isv-project.yaml --agent-work-dir .gapctl/work/<domain> --out-dir validation-bundle/")
    else:
        print(f"\n{open_gaps} gap(s) remaining.")
    return 0 if open_gaps == 0 else 1


# ── helpers ───────────────────────────────────────────────────────────────────


def _first_provider_config(report: GapReport, provider_repo: Path) -> Path | None:
    for row in report.rows:
        if row.evidence.config_path:
            path = Path(row.evidence.config_path)
            return path if path.is_absolute() else provider_repo / path
    return None
