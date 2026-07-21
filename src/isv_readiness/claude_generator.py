from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.generator_limits import CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS, MAX_GENERATOR_TIMEOUT_SECONDS
from isv_readiness.subprocesses import run_captured

ClaudeRunner = Callable[[Sequence[str], Path, str, int], subprocess.CompletedProcess[str]]

# Claude Code has no server-side constrained decoding, so the adapter enforces
# the change-set schema locally and feeds validator errors back for one retry.
MAX_ATTEMPTS = 2

# The Codex adapter gets isolation from `--sandbox read-only` in an empty
# temporary directory. Claude Code print mode instead gets every tool
# disallowed and a single turn: the request on stdin is the model's entire
# world, and its only act is to answer.
_TOOL_BAN = "*"


class ClaudeGeneratorError(ValueError):
    """Raised when the Claude adapter cannot return strict schema-valid JSON."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gapctl-claude-generator",
        description="Schema-validated Claude Code adapter for gapctl change-set generation",
    )
    parser.add_argument("--claude", default="claude", help="Claude Code CLI executable")
    parser.add_argument("--model", default=None, help="Optional explicit Claude model")
    parser.add_argument("--timeout", type=int, default=CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.read())
        result = generate_with_claude(
            request,
            claude_executable=args.claude,
            model=args.model,
            timeout_seconds=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"Claude model timed out after {exc.timeout} seconds.", file=sys.stderr)
        return 124
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, ClaudeGeneratorError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n")
    return 0


def generate_with_claude(
    request: dict[str, Any],
    *,
    claude_executable: str = "claude",
    model: str | None = None,
    timeout_seconds: int = CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS,
    runner: ClaudeRunner | None = None,
) -> dict[str, Any]:
    schema = request.get("output_schema")
    if not isinstance(schema, dict):
        raise ClaudeGeneratorError("Generator request does not contain an output_schema object.")
    if timeout_seconds < 1 or timeout_seconds > MAX_GENERATOR_TIMEOUT_SECONDS:
        raise ClaudeGeneratorError(
            f"Claude generator timeout must be between 1 and {MAX_GENERATOR_TIMEOUT_SECONDS} seconds."
        )
    validator = jsonschema.Draft202012Validator(schema)
    run = runner or _default_runner

    prompt = _initial_prompt(request)
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="gapctl-claude-") as tempdir:
        root = Path(tempdir)
        for attempt in range(MAX_ATTEMPTS):
            command = [
                claude_executable,
                "-p",
                "--output-format",
                "json",
                "--max-turns",
                "1",
                "--disallowedTools",
                _TOOL_BAN,
            ]
            if model:
                command.extend(["--model", model])
            completed = run(command, root, prompt, timeout_seconds)
            if completed.returncode != 0:
                details = (completed.stderr or completed.stdout or "").strip()
                raise ClaudeGeneratorError(
                    f"Claude generator exited with code {completed.returncode}: {details[:2000] or 'no output'}"
                )
            text = _result_text(completed.stdout or "")
            candidate = _extract_json_object(text)
            if candidate is None:
                failures = ["Response did not contain one JSON object."]
            else:
                _fill_content_hashes(candidate)
                errors = sorted(validator.iter_errors(candidate), key=lambda error: list(error.path))
                if not errors:
                    return candidate
                failures = [f"{error.json_path}: {error.message}" for error in errors[:10]]
            if attempt + 1 < MAX_ATTEMPTS:
                prompt = _retry_prompt(request, text, failures)
    raise ClaudeGeneratorError(
        "Claude output failed change-set schema validation: " + "; ".join(failures[:5])
    )


def _initial_prompt(request: dict[str, Any]) -> str:
    return (
        "You are a change-set generator adapter for gapctl. Read the JSON request below. "
        "Respond with exactly one JSON object that validates against request.output_schema. "
        "No markdown fences, no commentary, no explanation - only the JSON object. "
        "For every change, set content_sha256 to 64 zeros; the adapter computes the real "
        "hash over your content field.\n\n"
        + json.dumps(request, sort_keys=True, ensure_ascii=False)
    )


def _retry_prompt(request: dict[str, Any], previous: str, failures: Sequence[str]) -> str:
    return (
        "Your previous response was rejected by the change-set schema validator.\n"
        "Errors:\n- " + "\n- ".join(failures) + "\n\n"
        "Previous response (truncated):\n" + previous[:4000] + "\n\n"
        "Produce a corrected response for the same request. Respond with exactly one JSON "
        "object that validates against request.output_schema - nothing else.\n\n"
        + json.dumps(request, sort_keys=True, ensure_ascii=False)
    )


def _fill_content_hashes(candidate: dict[str, Any]) -> None:
    """Compute real per-file content hashes over the model's output.

    A language model cannot compute SHA-256, so the adapter owns the
    transport-integrity hash: it is a checksum of what the adapter emits,
    verified again by the harness at propose/apply time.
    """
    changes = candidate.get("changes")
    if not isinstance(changes, list):
        return
    for change in changes:
        if isinstance(change, dict) and isinstance(change.get("content"), str):
            change["content_sha256"] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()


def _result_text(stdout: str) -> str:
    """Unwrap `claude -p --output-format json` envelope; tolerate raw output."""
    stripped = stdout.strip()
    try:
        envelope = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
        return envelope["result"]
    return stripped


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            return None
        try:
            value, _ = json.JSONDecoder().raw_decode(stripped[start:])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


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
