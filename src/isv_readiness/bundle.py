from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.agent import load_agent_state, project_identity
from isv_readiness.project import load_project
from isv_readiness.schema import load_schema

BUNDLE_VERSION = "0.1.0"
ARTIFACT_PREFIXES = {
    "application-": "application",
    "gaps-": "report",
    "live-": "live",
    "live-gaps-": "live_report",
    "proposal-": "proposal",
    "rollback-": "rollback",
    "verification-": "verification",
}
PROVIDER_EXTENSIONS = {".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}


class BundleError(ValueError):
    """Raised when a reproducible bundle cannot be assembled safely."""


@dataclass(frozen=True)
class FileHash:
    path: str
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True)
class BundleManifest:
    schema_version: str
    provider: str
    outcome: str
    validation: dict[str, str | None]
    domains: tuple[dict[str, str], ...]
    provider_files: tuple[FileHash, ...]
    files: tuple[FileHash, ...]
    excluded_sensitive: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["domains"] = list(self.domains)
        payload["provider_files"] = [asdict(item) for item in self.provider_files]
        payload["files"] = [asdict(item) for item in self.files]
        payload["excluded_sensitive"] = list(self.excluded_sensitive)
        return payload


def load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Load and validate an existing evidence-bundle manifest."""
    manifest_path = bundle_dir.expanduser().resolve() / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise BundleError(f"Bundle manifest not found: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"Bundle manifest is not valid JSON: {manifest_path}") from exc
    _validate_bundle(raw)
    return raw


def build_bundle(
    project_path: Path,
    *,
    agent_work_dirs: list[Path],
    output_dir: Path,
    commit_resolver: Callable[[Path], str] | None = None,
) -> BundleManifest:
    project_path = project_path.expanduser().resolve()
    project = load_project(project_path)
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise BundleError(f"Refusing to overwrite existing bundle directory: {output_dir}")
    output_dir.mkdir(parents=True)

    states = []
    seen_domains = set()
    for raw_dir in agent_work_dirs:
        work_dir = raw_dir.expanduser().resolve()
        state_path = work_dir / "agent-state.json"
        if not state_path.is_file():
            raise BundleError(f"Agent state not found: {state_path}")
        state = load_agent_state(state_path)
        if state.project_sha256 != project_identity(project, project_path):
            raise BundleError(f"Agent state does not match the current project/profile: {state_path}")
        if state.domain in seen_domains:
            raise BundleError(f"Duplicate agent state for domain: {state.domain}")
        if state.domain not in project.assessment.domains:
            raise BundleError(f"Agent state domain is outside project scope: {state.domain}")
        seen_domains.add(state.domain)
        states.append((work_dir, state_path, state))

    files: list[FileHash] = []
    files.append(_copy_file(project_path, output_dir / "isv-project.yaml", output_dir, "project"))
    if project.assessment.profile:
        profile = project.resolve_path(project_path, project.assessment.profile)
        files.append(_copy_file(profile, output_dir / "solution-profile.yaml", output_dir, "profile"))

    domain_rows = []
    for work_dir, state_path, state in sorted(states, key=lambda item: item[2].domain):
        destination = output_dir / "domains" / state.domain
        files.append(_copy_file(state_path, destination / "agent-state.json", output_dir, "agent_state"))
        domain_rows.append({"domain": state.domain, "status": state.status, "reason": state.reason})
        for source in sorted(work_dir.iterdir()):
            kind = _artifact_kind(source.name)
            if kind and source.is_file() and not source.is_symlink():
                files.append(_copy_file(source, destination / source.name, output_dir, kind))

    provider_files = _provider_inventory(project.provider_root(project_path))
    complete = set(project.assessment.domains) == seen_domains and all(
        state.status == "complete" for _, _, state in states
    )
    outcome = "validation_complete" if complete else "incomplete"
    current_commit = (commit_resolver or _resolve_commit)(project.validation_root(project_path))
    if project.validation.resolved_commit and current_commit != project.validation.resolved_commit:
        raise BundleError("Validation checkout no longer matches the pinned project commit.")
    manifest = BundleManifest(
        schema_version=BUNDLE_VERSION,
        provider=project.provider.name,
        outcome=outcome,
        validation={
            "url": project.validation.url,
            "ref": project.validation.ref,
            "pinned_commit": project.validation.resolved_commit,
            "current_commit": current_commit,
        },
        domains=tuple(domain_rows),
        provider_files=tuple(provider_files),
        files=tuple(sorted(files, key=lambda item: item.path)),
        excluded_sensitive=(
            "context caches and context packs",
            "raw API specifications",
            "generated change-set source payloads",
            "credential values and process environments",
            "backup file contents",
            "raw command logs",
        ),
    )
    _validate_bundle(manifest.to_dict())
    (output_dir / "bundle-manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(_bundle_readme(manifest), encoding="utf-8")
    return manifest


def _copy_file(source: Path, target: Path, bundle_root: Path, kind: str) -> FileHash:
    if not source.is_file() or source.is_symlink():
        raise BundleError(f"Bundle source is not a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return FileHash(
        path=target.relative_to(bundle_root).as_posix(),
        kind=kind,
        sha256=_file_sha256(target),
        size=target.stat().st_size,
    )


def _provider_inventory(provider_root: Path) -> list[FileHash]:
    if not provider_root.is_dir():
        raise BundleError(f"Provider repository is missing: {provider_root}")
    files = []
    for path in sorted(provider_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in PROVIDER_EXTENSIONS:
            continue
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        files.append(
            FileHash(
                path=path.relative_to(provider_root).as_posix(),
                kind="provider_inventory",
                sha256=_file_sha256(path),
                size=path.stat().st_size,
            )
        )
    return files


def _artifact_kind(name: str) -> str | None:
    # Check the more-specific live report prefix before the general live prefix.
    for prefix in sorted(ARTIFACT_PREFIXES, key=len, reverse=True):
        if name.startswith(prefix):
            return ARTIFACT_PREFIXES[prefix]
    return None


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
        raise BundleError(f"Could not resolve validation commit: {(result.stderr or '').strip()}")
    return (result.stdout or "").strip()


def _validate_bundle(raw: Any) -> None:
    try:
        jsonschema.validate(raw, load_schema("bundle-manifest.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "bundle"
        raise BundleError(f"Invalid bundle manifest at {location}: {exc.message}") from exc


def _bundle_readme(manifest: BundleManifest) -> str:
    lines = [
        "# ISV Readiness Evidence Bundle",
        "",
        f"- Provider: `{manifest.provider}`",
        f"- Outcome: `{manifest.outcome}`",
        f"- Validation commit: `{manifest.validation['current_commit']}`",
        "",
        "## Owned domains",
        "",
    ]
    lines.extend(f"- `{item['domain']}`: {item['status']} — {item['reason']}" for item in manifest.domains)
    lines.extend(
        [
            "",
            "The manifest hashes every included artifact and inventories provider files without copying provider source.",
            "Raw context, credentials, model inputs, backups, and command logs are intentionally excluded.",
            "",
        ]
    )
    return "\n".join(lines)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
