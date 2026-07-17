from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.context import redact_text
from isv_readiness.decision import decide_gap, validation_profile_issues
from isv_readiness.onboarding import DOMAIN_CONFIG_FILES
from isv_readiness.project import ReadinessProject
from isv_readiness.scan.dynamic import DynamicArtifacts, scan_dynamic_artifacts
from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.models import GapReport
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import canonicalize_domain, load_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter

LIVE_RUN_VERSION = "0.1.0"
SELECTION_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]+$")
LiveRunner = Callable[[Sequence[str], Path, Mapping[str, str], int], subprocess.CompletedProcess[str]]
CommitResolver = Callable[[Path], str]


class LiveRunError(ValueError):
    """Raised when a live validation run is not explicitly and safely authorized."""


@dataclass(frozen=True)
class LiveRunResult:
    schema_version: str
    domain: str
    config: str
    selection: str | None
    command: tuple[str, ...]
    exit_code: int
    junit_path: str | None
    log_path: str
    selected_statuses: tuple[str, ...]
    success: bool
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["command"] = list(self.command)
        payload["selected_statuses"] = list(self.selected_statuses)
        return payload


def run_live_domain(
    project: ReadinessProject,
    manifest_path: Path,
    *,
    domain: str,
    artifacts_dir: Path,
    explicit_authorization: bool,
    selection: str | None = None,
    scope_path: Path | None = None,
    runner: LiveRunner | None = None,
    commit_resolver: CommitResolver | None = None,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 3600,
) -> LiveRunResult:
    canonical_domain = canonicalize_domain(domain)
    if not explicit_authorization:
        raise LiveRunError("Live validation requires the explicit --run-live flag.")
    if not project.execution.allow_live_runs:
        raise LiveRunError("Project policy disables live runs; review and set execution.allow_live_runs first.")
    if canonical_domain not in project.assessment.domains:
        raise LiveRunError(f"Domain '{canonical_domain}' is outside the selected project scope.")
    profile = (
        load_solution_profile(project.resolve_path(manifest_path, project.assessment.profile))
        if project.assessment.profile
        else None
    )
    profile_issues = validation_profile_issues(profile, [canonical_domain])
    if profile_issues:
        raise LiveRunError("Validation profile is not ready: " + "; ".join(profile_issues))
    if selection and not SELECTION_RE.fullmatch(selection):
        raise LiveRunError(f"Unsafe validation selection: {selection}")
    if timeout_seconds < 1 or timeout_seconds > 14_400:
        raise LiveRunError("Live run timeout must be between 1 and 14400 seconds.")

    validation_root = project.validation_root(manifest_path)
    provider_root = project.provider_root(manifest_path)
    expected_commit = project.validation.resolved_commit
    actual_commit = (commit_resolver or _resolve_commit)(validation_root)
    if expected_commit and actual_commit != expected_commit:
        raise LiveRunError(
            f"Validation checkout drifted from pinned commit {expected_commit} to {actual_commit}; re-bootstrap or review."
        )
    config = _domain_config(provider_root, canonical_domain)
    if not config.is_file():
        raise LiveRunError(f"Runnable provider config not found: {config}")

    source_env = environment or os.environ
    missing = sorted(name for name in project.execution.credential_env if not source_env.get(name))
    if missing:
        raise LiveRunError("Required credential environment variables are unset: " + ", ".join(missing))
    child_env = {
        name: source_env[name]
        for name in ("HOME", "PATH", "SSL_CERT_FILE", "TMPDIR")
        if source_env.get(name)
    }
    for name in (*project.execution.credential_env, *project.execution.pass_env):
        if source_env.get(name):
            child_env[name] = source_env[name]
    for api in project.apis:
        if canonical_domain in api.domains and api.base_url and api.base_url_env:
            child_env[api.base_url_env] = api.base_url

    artifacts_dir = artifacts_dir.expanduser().resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    junit = artifacts_dir / f"junit-{canonical_domain}.xml"
    log = artifacts_dir / f"isvctl-{canonical_domain}.log"
    try:
        config_arg = str(config.resolve().relative_to(validation_root))
    except ValueError:
        config_arg = str(config.resolve())
    command = [
        *IsvctlAdapter(validation_root).command_prefix,
        "test",
        "run",
        "-f",
        config_arg,
        "--no-upload",
        "--junitxml",
        str(junit),
    ]
    if selection:
        command.extend(["--", "-k", selection])
    result = (runner or _default_runner)(command, validation_root, child_env, timeout_seconds)
    log.write_text(redact_text(result.stdout or ""), encoding="utf-8")

    static_report = scan_provider(
        ScanOptions(provider_repo=provider_root, domains=[canonical_domain], validation_root=validation_root)
    )
    dynamic_rows = []
    if junit.is_file():
        if canonical_domain == "kubernetes":
            effective_scope = scope_path or (provider_root / "isv-readiness.k8s.scope.json")
            dynamic_rows = scan_k8s_artifacts(
                K8sDynamicArtifacts(
                    provider_repo=provider_root,
                    junit_path=junit,
                    log_path=log,
                    config_path=config,
                    scope=load_k8s_scope(effective_scope if effective_scope.is_file() else None),
                    static_rows=tuple(static_report.rows),
                )
            )
            dynamic_rows = [replace(row, domain="kubernetes") for row in dynamic_rows]
        else:
            dynamic_rows = scan_dynamic_artifacts(
                DynamicArtifacts(
                    provider_repo=provider_root,
                    domain=canonical_domain,
                    junit_path=junit,
                    log_path=log,
                    config_path=config,
                    static_rows=tuple(static_report.rows),
                )
            )
    report = GapReport(
        schema_version=static_report.schema_version,
        provider_repo=static_report.provider_repo,
        domains=static_report.domains,
        rows=sorted(
            [*static_report.rows, *dynamic_rows],
            key=lambda row: (row.domain, row.step_name, row.validation_class or "", row.detection, row.id),
        ),
    )
    if profile is not None:
        report = enrich_report_with_profile(report, profile)

    selected_rows = [
        row
        for row in report.rows
        if row.detection == "dynamic"
        if selection is None or _same_validation(selection, row.validation_class)
    ]
    statuses = tuple(sorted(row.status for row in selected_rows))
    # Success requires both an actual execution and the same reviewed
    # status/profile decision used by fill, status, and bundle.
    success = (
        result.returncode == 0
        and any(status == "pass" for status in statuses)
        and not any(decide_gap(row.to_dict()).blocking for row in selected_rows)
    )
    live_result = LiveRunResult(
        schema_version=LIVE_RUN_VERSION,
        domain=canonical_domain,
        config=config_arg,
        selection=selection,
        command=tuple(command),
        exit_code=result.returncode,
        junit_path=str(junit) if junit.is_file() else None,
        log_path=str(log),
        selected_statuses=statuses,
        success=success,
        report=report.to_dict(),
    )
    _validate_live_result(live_result.to_dict())
    return live_result


def _domain_config(provider_root: Path, domain: str) -> Path:
    if domain == "kubernetes":
        for suffix in (".yaml", ".yml"):
            wrapper = provider_root.with_suffix(suffix)
            if wrapper.is_file():
                return wrapper
        return provider_root.with_suffix(".yaml")
    filename = DOMAIN_CONFIG_FILES.get(domain)
    if filename is None:
        raise LiveRunError(f"No provider config mapping for domain: {domain}")
    return provider_root / "config" / filename


def _same_validation(selection: str, validation_class: str | None) -> bool:
    if not validation_class:
        return False
    return (
        selection == validation_class
        or selection.startswith(f"{validation_class}-")
        or validation_class.startswith(f"{selection}-")
    )


def _validate_live_result(raw: Any) -> None:
    try:
        jsonschema.validate(raw, load_schema("live-run.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "live_run"
        raise LiveRunError(f"Invalid live run at {location}: {exc.message}") from exc


def _resolve_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise LiveRunError(f"Could not resolve validation checkout commit: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout_seconds,
    )
