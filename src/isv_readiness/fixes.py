from __future__ import annotations

import ast
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from isv_readiness.decision import FAILURE_STATUSES, FIX_ACTION

FIXABLE_STATUSES = set(FAILURE_STATUSES)
ALLOWED_ACTION = FIX_ACTION

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
INSECURE_TLS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unverified SSL context", re.compile(r"\bssl\._create_unverified_context\s*\(")),
    ("disabled certificate verification", re.compile(r"\bssl\.CERT_NONE\b")),
    ("disabled hostname verification", re.compile(r"\bcheck_hostname\s*=\s*False\b")),
    ("disabled request verification", re.compile(r"\bverify\s*=\s*False\b")),
    ("insecure curl option", re.compile(r"(?m)^\s*curl\b[^\n]*(?:\s-k(?:\s|$)|--insecure\b)")),
)
INSECURE_SSH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "disabled SSH host-key verification",
        re.compile(r"(?i)StrictHostKeyChecking\s*[=:]\s*(?:no|off)\b"),
    ),
    (
        "discarded SSH known-hosts state",
        re.compile(r"(?i)UserKnownHostsFile\s*[=:]\s*(?:/dev/null|none)\b"),
    ),
    (
        "automatic trust of unknown SSH host keys",
        re.compile(r"\b(?:paramiko\.)?AutoAddPolicy\s*\("),
    ),
)
RAW_EVIDENCE_FIELDS = frozenset(
    {
        "console_output",
        "log_excerpt",
        "output_snippet",
        "raw_output",
        "response_body",
        "response_headers",
        "stderr",
        "stdout",
    }
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


def validate_candidate_content(
    relative_target: Path,
    candidate_text: str,
    *,
    allowed_environment: Sequence[str] | None = None,
) -> None:
    """Apply the reusable content boundary used by single and multi-file fixes."""
    _reject_secrets(candidate_text)
    _validate_candidate_syntax(relative_target, candidate_text)
    _reject_insecure_tls(candidate_text)
    _reject_insecure_ssh(candidate_text)
    if relative_target.suffix.lower() == ".py":
        _reject_raw_evidence_fields(candidate_text)
    if allowed_environment is not None:
        _reject_undeclared_environment(
            relative_target,
            candidate_text,
            frozenset(allowed_environment),
        )


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


def _reject_insecure_tls(candidate_text: str) -> None:
    for label, pattern in INSECURE_TLS_PATTERNS:
        if pattern.search(candidate_text):
            raise FixGuardrailError(f"Candidate contains insecure TLS behavior ({label}).")


def _reject_insecure_ssh(candidate_text: str) -> None:
    for label, pattern in INSECURE_SSH_PATTERNS:
        if pattern.search(candidate_text):
            raise FixGuardrailError(f"Candidate contains insecure SSH behavior ({label}).")


def _reject_raw_evidence_fields(candidate_text: str) -> None:
    forbidden = sorted(_python_literal_mapping_fields(candidate_text).intersection(RAW_EVIDENCE_FIELDS))
    if forbidden:
        raise FixGuardrailError(
            "Candidate emits raw provider output in result JSON: " + ", ".join(forbidden)
        )


def _python_literal_mapping_fields(text: str) -> set[str]:
    if not text.strip():
        return set()
    tree = ast.parse(text)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            fields.update(
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        elif isinstance(node, ast.Assign):
            fields.update(filter(None, (_assigned_subscript_field(target) for target in node.targets)))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            field = _assigned_subscript_field(node.target)
            if field:
                fields.add(field)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            fields.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return fields


def _assigned_subscript_field(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Subscript):
        return None
    key = node.slice
    return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def _reject_undeclared_environment(
    relative_target: Path,
    candidate_text: str,
    allowed: frozenset[str],
) -> None:
    suffix = relative_target.suffix.lower()
    if suffix == ".py":
        names = _python_environment_names(candidate_text)
    elif suffix == ".sh":
        names = _shell_environment_names(candidate_text)
    else:
        return
    undeclared = sorted(names.difference(allowed))
    if undeclared:
        raise FixGuardrailError(
            "Candidate contains undeclared runtime environment variable(s): "
            + ", ".join(undeclared)
        )


def _python_environment_names(text: str) -> set[str]:
    if not text.strip():
        return set()
    tree = ast.parse(text)
    helper_parameters = _environment_helper_parameters(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                names.add(node.slice.value)
        if not isinstance(node, ast.Call):
            continue
        direct = _direct_environment_call_name(node)
        if direct:
            names.add(direct)
            continue
        helper = _call_name(node.func)
        helper_parameter = helper_parameters.get(helper)
        if helper_parameter is None:
            continue
        position, parameter_name = helper_parameter
        value: ast.AST | None = node.args[position] if len(node.args) > position else None
        if value is None:
            value = next(
                (keyword.value for keyword in node.keywords if keyword.arg == parameter_name),
                None,
            )
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            names.add(value.value)
    return names


def _environment_helper_parameters(tree: ast.AST) -> dict[str, tuple[int, str]]:
    helpers: dict[str, tuple[int, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = [argument.arg for argument in node.args.args]
        for child in ast.walk(node):
            value: ast.AST | None = None
            if isinstance(child, ast.Subscript) and _is_os_environ(child.value):
                value = child.slice
            elif isinstance(child, ast.Call) and _is_direct_environment_call(child):
                value = child.args[0] if child.args else None
            if isinstance(value, ast.Name) and value.id in parameters:
                helpers[node.name] = (parameters.index(value.id), value.id)
                break
    return helpers


def _direct_environment_call_name(node: ast.Call) -> str | None:
    if not _is_direct_environment_call(node) or not node.args:
        return None
    value = node.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _is_direct_environment_call(node: ast.Call) -> bool:
    function = node.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return function.value.id == "os" and function.attr == "getenv"
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "get"
        and _is_os_environ(function.value)
    )


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _shell_environment_names(text: str) -> set[str]:
    # Intentionally uppercase-only: all provider credential and API env vars must be uppercase
    # by convention. Lowercase references ($path, $home) are ignored; they represent shell
    # internals or script-local variables that are not part of the provider env contract.
    names = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)[^}]*\}", text))
    names.update(re.findall(r"(?<!\$)\$([A-Z_][A-Z0-9_]*)", text))
    return names
