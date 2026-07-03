from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.fixes import (
    ALLOWED_ACTION,
    FIXABLE_STATUSES,
    FixGuardrailError,
    select_gap,
    validate_candidate_content,
)
from isv_readiness.onboarding import DOMAIN_CONFIG_FILES
from isv_readiness.solution_profile import canonicalize_domain

CHANGE_PROPOSAL_VERSION = "0.1.0"
MAX_CHANGE_SET_BYTES = 2_000_000
ALLOWED_SCRIPT_SUFFIXES = {".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}


@dataclass(frozen=True)
class Change:
    target_root: str
    path: str
    operation: str
    content: str
    content_sha256: str
    rationale: str


@dataclass(frozen=True)
class ChangeSet:
    schema_version: str
    gap_id: str
    context_pack_sha256: str
    generator: dict[str, Any]
    summary: str
    changes: tuple[Change, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["changes"] = [asdict(change) for change in self.changes]
        return payload


@dataclass(frozen=True)
class ProposalFile:
    target_root: str
    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str


@dataclass(frozen=True)
class ChangeProposal:
    schema_version: str
    gap_id: str
    domain: str
    change_set_sha256: str
    patch_sha256: str
    patch: str
    files: tuple[ProposalFile, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        return payload


def load_change_set(path: Path) -> ChangeSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return change_set_from_dict(raw)


def change_set_from_dict(raw: Any) -> ChangeSet:
    validate_change_set(raw)
    return ChangeSet(
        schema_version=raw["schema_version"],
        gap_id=raw["gap_id"],
        context_pack_sha256=raw["context_pack_sha256"],
        generator=dict(raw["generator"]),
        summary=raw["summary"],
        changes=tuple(Change(**item) for item in raw["changes"]),
    )


def validate_change_set(raw: Any) -> None:
    schema = json.loads(_schema_path("change-set.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "change_set"
        raise FixGuardrailError(f"Invalid change set at {location}: {exc.message}") from exc
    paths = [(item["target_root"], item["path"]) for item in raw["changes"]]
    if len(paths) != len(set(paths)):
        raise FixGuardrailError("Change set contains duplicate target paths.")
    total = sum(len(item["content"].encode("utf-8")) for item in raw["changes"])
    if total > MAX_CHANGE_SET_BYTES:
        raise FixGuardrailError(f"Change set contains {total} bytes; maximum is {MAX_CHANGE_SET_BYTES}.")
    for item in raw["changes"]:
        actual = _sha256_bytes(item["content"].encode("utf-8"))
        if actual != item["content_sha256"]:
            raise FixGuardrailError(f"Change content hash does not match for {item['path']}.")


def build_change_proposal(
    report: dict[str, Any],
    *,
    provider_repo: Path,
    change_set: ChangeSet,
) -> ChangeProposal:
    validate_change_set(change_set.to_dict())
    row = select_gap(report, change_set.gap_id)
    _authorize_selected_gap(row, change_set.gap_id)
    domain_value = row.get("domain")
    if not isinstance(domain_value, str) or not domain_value:
        raise FixGuardrailError(f"Gap {change_set.gap_id} has no valid domain.")
    domain = canonicalize_domain(domain_value)
    provider_root = provider_repo.resolve()
    if not provider_root.is_dir():
        raise FixGuardrailError(f"Provider repository is not a directory: {provider_root}")

    patches: list[str] = []
    files: list[ProposalFile] = []
    changed_keys: set[tuple[str, str]] = set()
    for change in change_set.changes:
        target = resolve_change_target(provider_root, change, domain=domain)
        relative = Path(change.path)
        if target.is_symlink():
            raise FixGuardrailError(f"Target '{change.path}' is a symlink; refusing an ambiguous edit boundary.")
        exists = target.exists()
        if change.operation == "create" and exists:
            raise FixGuardrailError(f"Create target already exists: {change.path}")
        if change.operation == "replace" and not exists:
            raise FixGuardrailError(f"Replace target does not exist: {change.path}")
        if not target.parent.is_dir():
            raise FixGuardrailError(f"Change target parent directory does not exist: {target.parent}")
        validate_candidate_content(relative, change.content)
        original = target.read_text(encoding="utf-8") if exists else ""
        if original == change.content:
            raise FixGuardrailError(f"Change is identical to current target: {change.path}")
        logical = _logical_patch_path(provider_root, change)
        patch = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                change.content.splitlines(keepends=True),
                fromfile="/dev/null" if not exists else f"a/{logical}",
                tofile=f"b/{logical}",
            )
        )
        if not patch:
            raise FixGuardrailError(f"Change did not produce a patch: {change.path}")
        patches.append(patch)
        files.append(
            ProposalFile(
                target_root=change.target_root,
                path=change.path,
                operation=change.operation,
                before_sha256=_file_sha256(target) if exists else None,
                after_sha256=change.content_sha256,
            )
        )
        changed_keys.add((change.target_root, change.path))

    _require_primary_target(row, provider_root, domain, changed_keys)
    combined = "".join(patches)
    return ChangeProposal(
        schema_version=CHANGE_PROPOSAL_VERSION,
        gap_id=change_set.gap_id,
        domain=domain,
        change_set_sha256=canonical_sha256(change_set.to_dict()),
        patch_sha256=_sha256_bytes(combined.encode("utf-8")),
        patch=combined,
        files=tuple(files),
    )


def resolve_change_target(provider_root: Path, change: Change, *, domain: str) -> Path:
    provider_root = provider_root.resolve()
    raw = Path(change.path)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise FixGuardrailError(f"Change target must be a normalized relative path: {change.path}")
    if change.target_root == "provider":
        target = (provider_root / raw).resolve()
        try:
            target.relative_to(provider_root)
        except ValueError as exc:
            raise FixGuardrailError(f"Change target escapes provider repository: {change.path}") from exc
        _authorize_provider_path(raw, domain)
        return target
    if change.target_root == "providers":
        if domain != "kubernetes":
            raise FixGuardrailError("The providers-root wrapper boundary is Kubernetes-only.")
        expected = {f"{provider_root.name}.yaml", f"{provider_root.name}.yml"}
        if raw.as_posix() not in expected:
            raise FixGuardrailError(
                f"Providers-root changes may target only the selected provider wrapper: {', '.join(sorted(expected))}"
            )
        return (provider_root.parent / raw).resolve()
    raise FixGuardrailError(f"Unsupported target root: {change.target_root}")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _sha256_bytes(encoded)


def _authorize_selected_gap(row: dict[str, Any], gap_id: str) -> None:
    remediation = row.get("remediation") or {}
    routing = (row.get("enrichment") or {}).get("solution_profile") or {}
    if row.get("status") not in FIXABLE_STATUSES:
        raise FixGuardrailError(f"Gap {gap_id} has non-fixable status '{row.get('status')}'.")
    if routing.get("action") != ALLOWED_ACTION:
        action = routing.get("action", "<missing>")
        raise FixGuardrailError(f"Gap {gap_id} routes to '{action}', not '{ALLOWED_ACTION}'.")
    if remediation.get("auto_fixable") is not True:
        raise FixGuardrailError(f"Gap {gap_id} is not marked auto_fixable by the scanner.")
    if not isinstance(remediation.get("target"), str) or not remediation["target"]:
        raise FixGuardrailError(f"Gap {gap_id} has no remediation target.")


def _authorize_provider_path(path: Path, domain: str) -> None:
    if path.parts[0] == "scripts":
        if len(path.parts) < 3 or path.suffix.lower() not in ALLOWED_SCRIPT_SUFFIXES:
            raise FixGuardrailError(f"Provider script target is not an approved text file: {path}")
        return
    if path.parts[0] == "config":
        expected = DOMAIN_CONFIG_FILES.get(domain)
        if expected is None or len(path.parts) != 2 or path.name != expected:
            raise FixGuardrailError(
                f"Config changes may target only the selected domain file: config/{expected or '<none>'}"
            )
        return
    raise FixGuardrailError(f"Change target is outside provider scripts/ and selected config boundaries: {path}")


def _require_primary_target(
    row: dict[str, Any],
    provider_root: Path,
    domain: str,
    changed_keys: set[tuple[str, str]],
) -> None:
    value = str(row["remediation"]["target"])
    target = Path(value)
    if target.is_absolute():
        try:
            target = target.resolve().relative_to(provider_root)
        except ValueError:
            if domain == "kubernetes" and target.resolve().parent == provider_root.parent:
                key = ("providers", target.name)
            else:
                raise FixGuardrailError(f"Primary remediation target is outside the provider boundary: {value}")
        else:
            key = ("provider", target.as_posix())
    else:
        normalized = target.as_posix()
        if normalized in {f"{provider_root.name}.yaml", f"{provider_root.name}.yml"} and domain == "kubernetes":
            key = ("providers", normalized)
        else:
            key = ("provider", normalized)
    if key not in changed_keys:
        raise FixGuardrailError(f"Change set does not include the selected gap target: {key[1]}")


def _logical_patch_path(provider_root: Path, change: Change) -> str:
    if change.target_root == "provider":
        return f"{provider_root.name}/{Path(change.path).as_posix()}"
    return Path(change.path).as_posix()


def _schema_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / name


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())
