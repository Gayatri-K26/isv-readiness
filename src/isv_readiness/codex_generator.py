from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

CodexRunner = Callable[[Sequence[str], Path, str, int], subprocess.CompletedProcess[str]]


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
    prompt = json.dumps(request, sort_keys=True, ensure_ascii=False)
    run = runner or _default_runner
    with tempfile.TemporaryDirectory(prefix="gapctl-codex-") as tempdir:
        root = Path(tempdir)
        schema_path = root / "change-set.schema.json"
        output_path = root / "last-message.json"
        schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")
        command = [
            codex_executable,
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
            details = (completed.stderr or completed.stdout or "").strip()
            raise CodexGeneratorError(
                f"Codex generator exited with code {completed.returncode}: {details[:2000] or 'no output'}"
            )
        if not output_path.is_file():
            raise CodexGeneratorError("Codex did not write the schema-constrained final message.")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexGeneratorError("Codex final message was not one JSON object.") from exc
    if not isinstance(result, dict):
        raise CodexGeneratorError("Codex final message must be a JSON object.")
    return result


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    prompt: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
