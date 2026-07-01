from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FIXABLE_STATUSES = {"fail", "not_implemented", "error"}
ALLOWED_ACTION = "implement_or_fix_adapter"
MAX_CANDIDATE_BYTES = 1_000_000

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "literal credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|password)\b"
            r"\s*[:=]\s*[\"'][^\"']{8,}[\"']"
        ),
    ),
)


class FixGuardrailError(ValueError):
    """Raised when a proposed fix crosses a deterministic safety boundary."""


@dataclass(frozen=True)
class FixProposal:
    gap_id: str
    target: str
    patch: str
    patch_sha256: str
    creates_file: bool


def build_fix_proposal(
    report: dict[str, Any],
    *,
    gap_id: str,
    provider_repo: Path,
    candidate_path: Path,
) -> FixProposal:
    row = select_gap(report, gap_id)
    remediation = row.get("remediation") or {}
    routing = (row.get("enrichment") or {}).get("solution_profile") or {}

    if row.get("status") not in FIXABLE_STATUSES:
        raise FixGuardrailError(f"Gap {gap_id} has non-fixable status '{row.get('status')}'.")
    if routing.get("action") != ALLOWED_ACTION:
        action = routing.get("action", "<missing>")
        raise FixGuardrailError(
            f"Gap {gap_id} routes to '{action}', not '{ALLOWED_ACTION}'. Resolve scope before proposing code."
        )
    if remediation.get("auto_fixable") is not True:
        raise FixGuardrailError(f"Gap {gap_id} is not marked auto_fixable by the scanner.")

    target_value = remediation.get("target")
    if not isinstance(target_value, str) or not target_value:
        raise FixGuardrailError(f"Gap {gap_id} has no remediation target.")

    provider_root = provider_repo.resolve()
    target = _resolve_provider_target(provider_root, target_value)
    relative_target = target.relative_to(provider_root)
    if not relative_target.parts or relative_target.parts[0] != "scripts":
        raise FixGuardrailError(f"Target '{relative_target}' is outside the approved provider scripts/ boundary.")
    if target.is_symlink():
        raise FixGuardrailError(f"Target '{relative_target}' is a symlink; refusing an ambiguous edit boundary.")

    candidate = candidate_path.read_bytes()
    if len(candidate) > MAX_CANDIDATE_BYTES:
        raise FixGuardrailError(f"Candidate is {len(candidate)} bytes; maximum allowed size is {MAX_CANDIDATE_BYTES}.")
    try:
        candidate_text = candidate.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FixGuardrailError("Candidate must be UTF-8 text.") from exc

    validate_candidate_content(relative_target, candidate_text)

    creates_file = not target.exists()
    original_text = target.read_text(encoding="utf-8") if target.exists() else ""
    if original_text == candidate_text:
        raise FixGuardrailError("Candidate is identical to the current target; no patch would be produced.")

    patch = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            candidate_text.splitlines(keepends=True),
            fromfile="/dev/null" if creates_file else f"a/{relative_target.as_posix()}",
            tofile=f"b/{relative_target.as_posix()}",
        )
    )
    if not patch:
        raise FixGuardrailError("Candidate did not produce a unified diff.")
    return FixProposal(
        gap_id=gap_id,
        target=relative_target.as_posix(),
        patch=patch,
        patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        creates_file=creates_file,
    )


def select_gap(report: dict[str, Any], gap_id: str) -> dict[str, Any]:
    matches = [row for row in report.get("rows", []) if row.get("id") == gap_id]
    if not matches:
        raise FixGuardrailError(f"Gap ID not found: {gap_id}")
    if len(matches) > 1:
        raise FixGuardrailError(f"Gap ID is not unique: {gap_id}")
    return matches[0]


def _resolve_provider_target(provider_root: Path, target_value: str) -> Path:
    target = Path(target_value)
    resolved = target.resolve() if target.is_absolute() else (provider_root / target).resolve()
    try:
        resolved.relative_to(provider_root)
    except ValueError as exc:
        raise FixGuardrailError(f"Target escapes provider repository: {target_value}") from exc
    return resolved


def _reject_secrets(candidate_text: str) -> None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(candidate_text):
            raise FixGuardrailError(f"Candidate contains secret-looking material ({label}).")


def validate_candidate_content(relative_target: Path, candidate_text: str) -> None:
    """Apply the reusable content boundary used by single and multi-file fixes."""
    _reject_secrets(candidate_text)
    _validate_candidate_syntax(relative_target, candidate_text)


def _validate_candidate_syntax(relative_target: Path, candidate_text: str) -> None:
    suffix = relative_target.suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(candidate_text)
        elif suffix == ".json":
            json.loads(candidate_text)
        elif suffix in {".yaml", ".yml"}:
            yaml.safe_load(candidate_text)
    except (SyntaxError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise FixGuardrailError(f"Candidate has invalid {suffix or 'text'} syntax: {exc}") from exc
