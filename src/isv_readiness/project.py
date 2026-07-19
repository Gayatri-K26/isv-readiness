from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

from isv_readiness.scan.k8s_onboard import PROVIDER_NAME_RE
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import (
    SUPPORTED_DOMAINS,
    SolutionProfile,
    SolutionProfileError,
    canonicalize_domain,
    load_solution_profile,
    parse_solution_profile,
)

PROJECT_SCHEMA_VERSION = "0.1.0"
DEFAULT_VALIDATION_URL = "https://github.com/NVIDIA/ai-cloud-validation.git"
DEFAULT_NSRG_URL = "https://docs.nvidia.com/dsx/ncp/llms.txt"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

ProviderState = Literal["new", "existing"]
CommandRunner = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[str]]


class ProjectError(ValueError):
    """Raised when a project manifest or bootstrap operation is unsafe or invalid."""


@dataclass(frozen=True)
class ValidationCheckout:
    url: str
    ref: str
    checkout: str
    resolved_commit: str | None


@dataclass(frozen=True)
# whether the provider dir exists or not
class ProviderProject:
    name: str
    path: str
    state: ProviderState


@dataclass(frozen=True)
class Assessment:
    domains: tuple[str, ...]
    profile: str | None


@dataclass(frozen=True)
class ApiInterface:
    id: str
    kind: str
    base_url: str | None
    base_url_env: str | None
    spec: str | None
    auth_env: tuple[str, ...]
    domains: tuple[str, ...]


@dataclass(frozen=True)
class ContextSource:
    id: str
    kind: str
    location: str
    trust: str
    required: bool
    domains: tuple[str, ...]
    labels: tuple[str, ...]
    query: str | None


@dataclass(frozen=True)
class ExecutionPolicy:
    run_environment: str
    allow_live_runs: bool
    credential_env: tuple[str, ...]
    pass_env: tuple[str, ...]
    max_attempts: int
    cleanup_required: bool


@dataclass(frozen=True)
class ReadinessProject:
    schema_version: str
    validation: ValidationCheckout
    provider: ProviderProject
    assessment: Assessment
    apis: tuple[ApiInterface, ...]
    context_sources: tuple[ContextSource, ...]
    execution: ExecutionPolicy

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def resolve_path(self, manifest_path: Path, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (manifest_path.resolve().parent / path).resolve()

    def validation_root(self, manifest_path: Path) -> Path:
        return self.resolve_path(manifest_path, self.validation.checkout)

    def provider_root(self, manifest_path: Path) -> Path:
        return self.resolve_path(manifest_path, self.provider.path)


@dataclass(frozen=True)
class BootstrapPlan:
    manifest_path: Path
    validation_root: Path
    validation_url: str
    validation_ref: str
    provider_name: str
    domains: tuple[str, ...]
    profile: Path | None
    api_base_url: str | None
    api_base_url_env: str | None
    api_spec: str | None
    auth_env: tuple[str, ...]
    pass_env: tuple[str, ...]
    clone_required: bool

    def summary_lines(self) -> list[str]:
        action = "clone" if self.clone_required else "use existing checkout"
        return [
            f"Project: {self.manifest_path}",
            f"Validation checkout: {self.validation_root} ({action})",
            f"Validation source: {self.validation_url} @ {self.validation_ref}",
            f"Provider: {self.provider_name}",
            "Phase: qualify (assess & scope) — enter validate after SME profile review",
            "Owned domains: " + ", ".join(self.domains),
            "Live infrastructure runs: require explicit confirmation during gapctl validate",
        ]


def build_bootstrap_plan(
    workspace: Path,
    *,
    provider_name: str,
    domains: Sequence[str],
    validation_root: Path | None = None,
    validation_url: str = DEFAULT_VALIDATION_URL,
    validation_ref: str = "main",
    profile: Path | None = None,
    api_base_url: str | None = None,
    api_base_url_env: str | None = None,
    api_spec: str | None = None,
    auth_env: Sequence[str] = (),
    pass_env: Sequence[str] = (),
) -> BootstrapPlan:
    if not PROVIDER_NAME_RE.fullmatch(provider_name):
        raise ProjectError("Provider name must contain only lowercase letters, numbers, '_' and '-'.")
    normalized_domains = tuple(dict.fromkeys(canonicalize_domain(item.strip()) for item in domains if item.strip()))
    if not normalized_domains:
        raise ProjectError("At least one assessment domain is required.")
    unsupported = sorted(set(normalized_domains).difference(SUPPORTED_DOMAINS))
    if unsupported:
        raise ProjectError(f"Unsupported assessment domains: {', '.join(unsupported)}")
    if not validation_url.strip() or not validation_ref.strip():
        raise ProjectError("Validation URL and ref must not be empty.")
    declared_env = [*auth_env, *pass_env, *([api_base_url_env] if api_base_url_env else [])]
    invalid_env = [name for name in declared_env if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)]
    if invalid_env:
        raise ProjectError(f"Credential inputs must be environment variable names: {', '.join(invalid_env)}")

    workspace = workspace.expanduser().resolve()
    checkout = (validation_root or (workspace / "ai-cloud-validation")).expanduser().resolve()
    return BootstrapPlan(
        manifest_path=workspace / "isv-project.yaml",
        validation_root=checkout,
        validation_url=validation_url.strip(),
        validation_ref=validation_ref.strip(),
        provider_name=provider_name,
        domains=normalized_domains,
        profile=profile.expanduser().resolve() if profile else None,
        api_base_url=api_base_url.strip() if api_base_url else None,
        api_base_url_env=api_base_url_env,
        api_spec=api_spec.strip() if api_spec else None,
        auth_env=tuple(dict.fromkeys(auth_env)),
        pass_env=tuple(dict.fromkeys(pass_env)),
        clone_required=not checkout.exists(),
    )


def execute_bootstrap(
    plan: BootstrapPlan,
    *,
    overwrite: bool = False,
    runner: CommandRunner | None = None,
    timeout_seconds: int = 600,
) -> ReadinessProject:
    if plan.manifest_path.exists() and not overwrite:
        raise ProjectError(f"Refusing to overwrite existing project: {plan.manifest_path}")
    supplied_profile = _validate_supplied_profile(plan) if plan.profile else None
    plan.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    run = runner or _default_runner
    if plan.clone_required:
        result = run(
            (
                "git",
                "clone",
                "--branch",
                plan.validation_ref,
                "--single-branch",
                plan.validation_url,
                str(plan.validation_root),
            ),
            plan.manifest_path.parent,
            timeout_seconds,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise ProjectError(f"Validation checkout clone failed: {details or 'no output'}")
    _validate_checkout(plan.validation_root)
    commit_result = run(("git", "rev-parse", "HEAD"), plan.validation_root, 30)
    commit = (commit_result.stdout or "").strip()
    if commit_result.returncode != 0 or not COMMIT_RE.fullmatch(commit):
        raise ProjectError("Could not resolve the validation checkout to an exact commit.")

    providers_dir = plan.validation_root / "isvctl" / "configs" / "providers"
    provider_path = providers_dir / plan.provider_name
    state: ProviderState = "existing" if provider_path.is_dir() else "new"
    manifest_dir = plan.manifest_path.parent
    checkout_value = _portable_path(plan.validation_root, manifest_dir)
    provider_value = _portable_path(provider_path, manifest_dir)
    profile_path = plan.profile or (manifest_dir / "solution-profile.yaml")
    profile_value = _portable_path(profile_path, manifest_dir)

    apis: tuple[ApiInterface, ...] = ()
    sources = [
        ContextSource(
            id="nsrg",
            kind="web_url",
            location=DEFAULT_NSRG_URL,
            trust="reference",
            required=True,
            domains=plan.domains,
            labels=(),
            query=None,
        ),
    ]
    if plan.api_base_url or plan.api_spec:
        apis = (
            ApiInterface(
                id="primary_api",
                kind="rest",
                base_url=plan.api_base_url,
                base_url_env=plan.api_base_url_env,
                spec=plan.api_spec,
                auth_env=plan.auth_env,
                domains=plan.domains,
            ),
        )
    if plan.api_spec:
        sources.append(
            ContextSource(
                id="primary_api_spec",
                kind="api_spec",
                location=plan.api_spec,
                trust="authoritative",
                required=True,
                domains=plan.domains,
                labels=(),
                query=None,
            )
        )

    project = ReadinessProject(
        schema_version=PROJECT_SCHEMA_VERSION,
        validation=ValidationCheckout(
            url=plan.validation_url,
            ref=plan.validation_ref,
            checkout=checkout_value,
            resolved_commit=commit,
        ),
        provider=ProviderProject(name=plan.provider_name, path=provider_value, state=state),
        assessment=Assessment(domains=plan.domains, profile=profile_value),
        apis=apis,
        context_sources=tuple(sources),
        execution=ExecutionPolicy(
            run_environment="not_configured",
            allow_live_runs=False,
            credential_env=plan.auth_env,
            pass_env=plan.pass_env,
            max_attempts=3,
            cleanup_required=True,
        ),
    )
    validate_project(project.to_dict())
    if plan.profile is None:
        if profile_path.exists() and not overwrite:
            raise ProjectError(f"Refusing to overwrite existing generated profile: {profile_path}")
        draft_profile = _draft_scope_profile(plan)
        parse_solution_profile(draft_profile)
        profile_path.write_text(yaml.safe_dump(draft_profile, sort_keys=False), encoding="utf-8")
    elif supplied_profile is None:
        raise ProjectError("Explicit profile validation did not produce a profile.")
    plan.manifest_path.write_text(yaml.safe_dump(project.to_dict(), sort_keys=False), encoding="utf-8")
    return project


def load_project(path: Path) -> ReadinessProject:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_project(raw)
    return ReadinessProject(
        schema_version=raw["schema_version"],
        validation=ValidationCheckout(**raw["validation"]),
        provider=ProviderProject(**raw["provider"]),
        assessment=Assessment(
            domains=tuple(raw["assessment"]["domains"]),
            profile=raw["assessment"]["profile"],
        ),
        apis=tuple(
            ApiInterface(
                id=item["id"],
                kind=item["kind"],
                base_url=item["base_url"],
                base_url_env=item["base_url_env"],
                spec=item["spec"],
                auth_env=tuple(item["auth_env"]),
                domains=tuple(item["domains"]),
            )
            for item in raw["apis"]
        ),
        context_sources=tuple(
            ContextSource(
                id=item["id"],
                kind=item["kind"],
                location=item["location"],
                trust=item["trust"],
                required=item["required"],
                domains=tuple(item["domains"]),
                labels=tuple(item["labels"]),
                query=item["query"],
            )
            for item in raw["context_sources"]
        ),
        execution=ExecutionPolicy(
            run_environment=raw["execution"]["run_environment"],
            allow_live_runs=raw["execution"]["allow_live_runs"],
            credential_env=tuple(raw["execution"]["credential_env"]),
            pass_env=tuple(raw["execution"]["pass_env"]),
            max_attempts=raw["execution"]["max_attempts"],
            cleanup_required=raw["execution"]["cleanup_required"],
        ),
    )


def validate_project(raw: Any) -> None:
    try:
        jsonschema.validate(raw, load_schema("project.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "project"
        raise ProjectError(f"Invalid project at {location}: {exc.message}") from exc
    ids = [item["id"] for item in raw["apis"]] + [item["id"] for item in raw["context_sources"]]
    if len(ids) != len(set(ids)):
        raise ProjectError("API and context source IDs must be unique across the project.")
    secrets = [value for value in raw["execution"]["credential_env"] if "=" in value]
    if secrets:
        raise ProjectError("Project files store credential environment variable names, never secret values.")
    environment_names = [
        *raw["execution"]["credential_env"],
        *raw["execution"]["pass_env"],
        *(item["base_url_env"] for item in raw["apis"] if item["base_url_env"]),
    ]
    invalid_env = [name for name in environment_names if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)]
    if invalid_env:
        raise ProjectError(f"Invalid environment variable names: {', '.join(invalid_env)}")


def _validate_checkout(root: Path) -> None:
    required = root / "isvctl" / "configs" / "providers" / "my-isv"
    if not (root / ".git").exists() or not required.is_dir():
        raise ProjectError(f"Not an ai-cloud-validation checkout: {root}")


def _validate_supplied_profile(plan: BootstrapPlan) -> SolutionProfile:
    assert plan.profile is not None
    if not plan.profile.is_file():
        raise ProjectError(f"Solution profile not found: {plan.profile}")
    try:
        profile = load_solution_profile(plan.profile)
    except (OSError, SolutionProfileError) as exc:
        raise ProjectError(f"Could not load solution profile: {exc}") from exc
    missing = sorted(domain for domain in plan.domains if profile.resolve(domain) is None)
    if missing:
        raise ProjectError(f"Solution profile does not cover owned domains: {', '.join(missing)}")
    return profile


def _draft_scope_profile(plan: BootstrapPlan) -> dict[str, Any]:
    solution_id = re.sub(r"[^a-z0-9_-]", "_", plan.provider_name.lower())
    if not solution_id[0].isalpha():
        solution_id = f"isv_{solution_id}"
    return {
        "schema_version": "0.1.0",
        "solution": {
            "id": solution_id,
            "name": plan.provider_name,
            "vendor": plan.provider_name,
            "version": "unknown",
            "profile_status": "draft",
            "target_environment": "not_configured",
        },
        "journey": {"stage": "qualify", "status": "in_progress"},
        "actors": [{"id": "isv", "name": plan.provider_name, "kind": "isv"}],
        "components": [
            {
                "id": "provider",
                "name": plan.provider_name,
                "version": "unknown",
                "kind": "product",
                "supplier_actor_id": "isv",
                "depends_on": [],
                "source_refs": [],
            }
        ],
        "domains": [
            {
                "domain": domain,
                "name": domain.replace("_", " ").title(),
                "owned": True,
                "coverage": "covered",
                "validation_mode": "test",
                "capability_owner_actor_id": "isv",
                "provider_adapter_owner_actor_id": "isv",
                "component_ids": ["provider"],
                "provider_configs": [],
                "rationale": "ISV-owned domain declared during the qualify phase at bootstrap.",
                "required_inputs": [],
                "evidence_refs": [],
                "capabilities": [],
            }
            for domain in plan.domains
        ],
        "sources": [],
        "assumptions": [
            "This draft records operator-declared owned scope only; an SME must review versions, capability ownership, and assign NSRG layers to components before entering the validate phase."
        ],
    }


def _portable_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _default_runner(command: Sequence[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
