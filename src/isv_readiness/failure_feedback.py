from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_FAILURE_SUMMARY_CHARS = 1_000
MAX_FAILURE_DETAIL_CHARS = 2_000
MAX_FAILURE_DETAILS = 10

SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*\s*[:=]\s*)([^\n]+)$"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")

_TIMESTAMP_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?Z?"
    r"|[0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?)\b"
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_LABELED_ID_RE = re.compile(
    r"(?i)\b(request|trace|correlation|resource|operation|task|job)[-_ ]?id"
    r"(\s*[:=]\s*)([A-Za-z0-9._:/-]+)"
)
_POLL_COUNT_RE = re.compile(
    r"(?i)\b(poll|attempt|retry)(?:\s+(?:count|number))?\s*(?:#|[:=])?\s*\d+\b"
)


def redact_failure_text(text: str) -> str:
    """Redact secrets and email-like PII while preserving diagnostic structure."""

    text = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = AWS_KEY_RE.sub("[REDACTED AWS KEY]", text)
    return EMAIL_RE.sub("[REDACTED EMAIL]", text)


def redact_failure_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_failure_text(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(
                marker in name.upper()
                for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")
            ):
                redacted[name] = "[REDACTED]"
            else:
                redacted[name] = redact_failure_value(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_failure_value(item) for item in value]
    return value


def bounded_failure_text(value: str, max_chars: int) -> str:
    value = redact_failure_text(value)
    if len(value) <= max_chars:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{value[:max_chars]}... [truncated; sha256 {digest}]"


def stable_failure_fingerprint(category: str, summary: str, details: Iterable[str]) -> str:
    """Fingerprint a root cause after removing values that change between runs."""

    canonical = json.dumps(
        {
            "category": _normalize_identity(category),
            "summary": _normalize_identity(redact_failure_text(summary)),
            "details": [
                _normalize_identity(redact_failure_text(detail))
                for detail in details
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def artifact_reference(kind: str, path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return {
        "kind": kind,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _normalize_identity(value: str) -> str:
    value = " ".join(value.split())
    value = _TIMESTAMP_RE.sub("<timestamp>", value)
    value = _UUID_RE.sub("<uuid>", value)
    value = _LABELED_ID_RE.sub(
        lambda match: f"{match.group(1).lower()}_id{match.group(2)}<id>",
        value,
    )
    return _POLL_COUNT_RE.sub(
        lambda match: f"{match.group(1).lower()} <count>",
        value,
    )
