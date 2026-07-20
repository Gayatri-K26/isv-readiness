from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from isv_readiness.subprocesses import run_captured

CodexRunner = Callable[[Sequence[str], Path, str, int], subprocess.CompletedProcess[str]]

_UNSUPPORTED_OUTPUT_SCHEMA_KEYWORDS = frozenset(
    {
        "default",
        "maxLength",
        "maxProperties",
        "minLength",
        "minProperties",
        "uniqueItems",
    }
)


class CodexGeneratorError(ValueError):
    """Raised when the reference Codex adapter cannot return strict JSON."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gapctl-codex-generator",
        description="Schema-constrained Codex adapter for gapctl change-set generation",
    )
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    parser.add_argument("--model", default=None, help="Optional explicit Codex model")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        result = generate_with_codex(
            request,
            codex_executable=args.codex,
            model=args.model,
            timeout_seconds=args.timeout,
        )
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, CodexGeneratorError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n")
    return 0


def generate_with_codex(
    request: dict[str, Any],
    *,
    codex_executable: str = "codex",
    model: str | None = None,
    timeout_seconds: int = 600,
    runner: CodexRunner | None = None,
) -> dict[str, Any]:
    schema = request.get("output_schema")
    if not isinstance(schema, dict):
        raise CodexGeneratorError("Generator request does not contain an output_schema object.")
    if timeout_seconds < 1 or timeout_seconds > 1800:
        raise CodexGeneratorError("Codex generator timeout must be between 1 and 1800 seconds.")
    codex_schema = _codex_output_schema(schema)
    codex_request = dict(request)
    codex_request["output_schema"] = codex_schema
    prompt = json.dumps(codex_request, sort_keys=True, ensure_ascii=False)
    run = runner or _default_runner
    with tempfile.TemporaryDirectory(prefix="gapctl-codex-") as tempdir:
        root = Path(tempdir)
        schema_path = root / "change-set.schema.json"
        output_path = root / "last-message.json"
        schema_path.write_text(json.dumps(codex_schema, sort_keys=True), encoding="utf-8")
        command = [
            _resolve_codex_executable(codex_executable),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if model:
            command.extend(["--model", model])
        command.append("-")
        completed = run(command, root, prompt, timeout_seconds)
        if completed.returncode != 0:
            details = _failure_details(completed)
            raise CodexGeneratorError(
                f"Codex generator exited with code {completed.returncode}: {details or 'no output'}"
            )
        if not output_path.is_file():
            raise CodexGeneratorError("Codex did not write the schema-constrained final message.")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexGeneratorError("Codex final message was not one JSON object.") from exc
        result = _remove_optional_nulls(result, schema)
    if not isinstance(result, dict):
        raise CodexGeneratorError("Codex final message must be a JSON object.")
    _fill_content_hashes(result)
    return result


def _codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a full JSON Schema into Codex's strict output subset.

    The product schema remains authoritative. This copy only removes keywords
    unsupported by Structured Outputs and represents optional object fields as
    required nullable fields. Optional nulls are removed from the response
    before the product validates it against the original schema.
    """

    converted = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        for keyword in _UNSUPPORTED_OUTPUT_SCHEMA_KEYWORDS:
            node.pop(keyword, None)
        for value in node.values():
            visit(value)

        if "type" not in node:
            if "const" in node:
                node["type"] = _json_type(node["const"])
            elif isinstance(node.get("enum"), list) and node["enum"]:
                enum_types = list(dict.fromkeys(_json_type(item) for item in node["enum"]))
                node["type"] = enum_types[0] if len(enum_types) == 1 else enum_types

        properties = node.get("properties")
        if isinstance(properties, dict):
            originally_required = set(node.get("required", []))
            for name, property_schema in list(properties.items()):
                if name not in originally_required:
                    properties[name] = _nullable(property_schema)
            node["required"] = list(properties)
            node["additionalProperties"] = False

    visit(converted)
    return converted


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object"


def _nullable(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"anyOf": [{}, {"type": "null"}]}
    schema_type = schema.get("type")
    if schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type):
        return schema
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and any(
        isinstance(item, dict) and item.get("type") == "null" for item in alternatives
    ):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _remove_optional_nulls(value: Any, schema: dict[str, Any]) -> Any:
    root = schema

    def clean(candidate: Any, candidate_schema: Any) -> Any:
        resolved = _resolve_schema(candidate_schema, root)
        if isinstance(candidate, dict):
            properties = resolved.get("properties", {}) if isinstance(resolved, dict) else {}
            required = set(resolved.get("required", [])) if isinstance(resolved, dict) else set()
            return {
                key: clean(item, properties.get(key, {}))
                for key, item in candidate.items()
                if item is not None or key in required
            }
        if isinstance(candidate, list):
            item_schema = resolved.get("items", {}) if isinstance(resolved, dict) else {}
            return [clean(item, item_schema) for item in candidate]
        return candidate

    return clean(value, schema)


def _resolve_schema(schema: Any, root: dict[str, Any]) -> Any:
    if not isinstance(schema, dict):
        return schema
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    resolved: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(resolved, dict):
            return schema
        resolved = resolved.get(part.replace("~1", "/").replace("~0", "~"))
    return resolved if isinstance(resolved, dict) else schema


def _resolve_codex_executable(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    if executable != "codex":
        return executable
    candidates = (
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path.home() / "Applications/Codex.app/Contents/Resources/codex",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise CodexGeneratorError(
        "Codex CLI was not found. Install Codex CLI or Codex.app, or pass --codex /path/to/codex."
    )


def _failure_details(completed: subprocess.CompletedProcess[str], limit: int = 1800) -> str:
    details = (completed.stderr or completed.stdout or "").strip()
    if len(details) <= limit:
        return details
    return "..." + details[-limit:]


def _fill_content_hashes(candidate: dict[str, Any]) -> None:
    """Compute real per-file content hashes over the model's output.

    A language model cannot compute SHA-256; the adapter owns the
    transport-integrity hash, and the harness verifies it again at
    propose/apply time.
    """
    changes = candidate.get("changes")
    if not isinstance(changes, list):
        return
    for change in changes:
        if isinstance(change, dict) and isinstance(change.get("content"), str):
            change["content_sha256"] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return run_captured(
        command,
        cwd=cwd,
        input_text=prompt,
        timeout_seconds=timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
