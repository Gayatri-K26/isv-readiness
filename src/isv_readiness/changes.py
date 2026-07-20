from __future__ import annotations

import ast
import difflib
import hashlib
import json
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from isv_readiness.decision import decide_gap
from isv_readiness.fixes import (
    FixGuardrailError,
    select_gap,
    validate_candidate_content,
)
from isv_readiness.onboarding import DOMAIN_CONFIG_FILES
from isv_readiness.schema import schema_path
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
    allowed_environment: Sequence[str] | None = None,
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
        original = target.read_text(encoding="utf-8") if exists else ""
        _validate_domain_config_edit_scope(
            row,
            change,
            original=original,
            domain=domain,
        )
        validate_candidate_content(
            relative,
            change.content,
            allowed_environment=allowed_environment,
        )
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
    _validate_timeout_envelopes(provider_root, domain, change_set)
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
    decision = decide_gap(row)
    if not decision.edit_eligible:
        raise FixGuardrailError(f"Gap {gap_id} is not eligible for generation: {decision.reason}")


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


def _validate_domain_config_edit_scope(
    row: dict[str, Any],
    change: Change,
    *,
    original: str,
    domain: str,
) -> None:
    """Require an existing domain config edit to stay inside one selected step."""

    config_name = DOMAIN_CONFIG_FILES.get(domain)
    if (
        change.target_root != "provider"
        or change.path != f"config/{config_name}"
        or change.operation != "replace"
    ):
        return

    step_name = row.get("step_name")
    if not isinstance(step_name, str) or not step_name or step_name.startswith("<"):
        raise FixGuardrailError("A domain config replacement requires one concrete selected step name.")

    original_without_step, original_count = _without_yaml_step(original, step_name)
    candidate_without_step, candidate_count = _without_yaml_step(change.content, step_name)
    if original_count > 1 or candidate_count != 1:
        raise FixGuardrailError(
            f"Domain config must contain exactly one selected step named '{step_name}' after the change."
        )
    if _without_trailing_newlines(original_without_step) != _without_trailing_newlines(candidate_without_step):
        raise FixGuardrailError(
            f"Config change for step '{step_name}' modifies text outside that step block; "
            "preserve comments, formatting, and unrelated steps exactly."
        )


def _without_yaml_step(source: str, step_name: str) -> tuple[str, int]:
    """Remove one block-list step while leaving all surrounding text byte-for-byte."""

    lines = source.splitlines(keepends=True)
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        marker = _yaml_step_marker(line)
        if marker is None or marker[1] != step_name:
            continue
        indent = marker[0]
        end = len(lines)
        for candidate_index in range(index + 1, len(lines)):
            candidate = lines[candidate_index]
            if len(candidate) - len(candidate.lstrip(" ")) == indent and candidate.lstrip(" ").startswith("- "):
                end = candidate_index
                break
        matches.append((index, end))

    if len(matches) != 1:
        return source, len(matches)
    start, end = matches[0]
    return "".join((*lines[:start], *lines[end:])), 1


def _yaml_step_marker(line: str) -> tuple[int, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    stripped = line[indent:]
    if not stripped.startswith("- "):
        return None
    try:
        value = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return None
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None
    name = value[0].get("name")
    return (indent, name) if isinstance(name, str) and name else None


def _without_trailing_newlines(value: str) -> str:
    return value.rstrip("\r\n")


def _validate_timeout_envelopes(
    provider_root: Path,
    domain: str,
    change_set: ChangeSet,
) -> None:
    """Reject a script whose own deadline is longer than its runner step."""

    config_name = DOMAIN_CONFIG_FILES.get(domain)
    if not config_name:
        return
    config_path = provider_root / "config" / config_name
    replacements = {
        change.path: change.content
        for change in change_set.changes
        if change.target_root == "provider"
    }
    config_text = replacements.get(f"config/{config_name}")
    if config_text is None:
        if not config_path.is_file():
            return
        config_text = config_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(config_text)
    if not isinstance(raw, dict):
        return
    command_groups = raw.get("commands")
    if not isinstance(command_groups, dict):
        return

    for group in command_groups.values():
        if not isinstance(group, dict):
            continue
        steps = group.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("timeout"), (int, float)):
                continue
            relative_script = _configured_python_script(config_path, step.get("command"))
            if relative_script is None:
                continue
            script_text = replacements.get(relative_script.as_posix())
            if script_text is None:
                script_path = provider_root / relative_script
                if not script_path.is_file():
                    continue
                script_text = script_path.read_text(encoding="utf-8")
            internal_timeout = _max_internal_timeout(script_text)
            runner_timeout = float(step["timeout"])
            if internal_timeout is not None and internal_timeout > runner_timeout:
                step_name = str(step.get("name") or relative_script)
                raise FixGuardrailError(
                    f"Candidate timeout mismatch for step {step_name}: internal deadline "
                    f"{internal_timeout:g}s exceeds configured timeout {runner_timeout:g}s."
                )


def _configured_python_script(config_path: Path, command: Any) -> Path | None:
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    script = next((token for token in tokens if token.endswith(".py")), None)
    if script is None:
        return None
    resolved = (config_path.parent / script).resolve()
    try:
        return resolved.relative_to(config_path.parent.parent.resolve())
    except ValueError:
        return None


def _max_internal_timeout(source: str) -> float | None:
    """Return the largest explicit operation deadline visible in a Python script."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    constants: dict[str, float] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = _numeric_value(node.value, constants)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    constants[target.id] = value

    deadlines: list[float] = []
    for name, value in constants.items():
        if "TIMEOUT" in name.upper():
            deadlines.append(value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
            continue
        if _is_clock_call(node.left):
            value = _numeric_value(node.right, constants)
            if value is not None:
                deadlines.append(value)
        elif _is_clock_call(node.right):
            value = _numeric_value(node.left, constants)
            if value is not None:
                deadlines.append(value)
    return max(deadlines) if deadlines else None


def _numeric_value(node: ast.AST | None, constants: dict[str, float]) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _is_clock_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
        and node.func.attr in {"monotonic", "time"}
    )


def _logical_patch_path(provider_root: Path, change: Change) -> str:
    if change.target_root == "provider":
        return f"{provider_root.name}/{Path(change.path).as_posix()}"
    return Path(change.path).as_posix()


def _schema_path(name: str) -> Path:
    return schema_path(name)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())
