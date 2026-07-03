from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import yaml

FIXABLE_STATUSES = {"fail", "not_implemented", "error"}
ALLOWED_ACTION = "implement_or_fix_adapter"

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


def select_gap(report: dict[str, Any], gap_id: str) -> dict[str, Any]:
    matches = [row for row in report.get("rows", []) if row.get("id") == gap_id]
    if not matches:
        raise FixGuardrailError(f"Gap ID not found: {gap_id}")
    if len(matches) > 1:
        raise FixGuardrailError(f"Gap ID is not unique: {gap_id}")
    return matches[0]


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
