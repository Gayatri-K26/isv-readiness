from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from isv_readiness.changes import ChangeSet, canonical_sha256, change_set_from_dict, validate_change_set
from isv_readiness.fixes import FixGuardrailError

GeneratorRunner = Callable[
    [Sequence[str], Path, str, Mapping[str, str], int],
    subprocess.CompletedProcess[str],
]


def dispatch_generator(
    request: dict[str, Any],
    *,
    command: Sequence[str],
    cwd: Path,
    pass_env: Sequence[str] = (),
    timeout_seconds: int = 300,
    runner: GeneratorRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a generator adapter under the shared guardrails and return its JSON object.

    This is the single enforcement point for the adapter contract: allowlisted
    child environment, no shell, request on stdin, exactly one JSON object on
    stdout. Callers own request composition and output validation.
    """
    if not command or not all(isinstance(item, str) and item for item in command):
        raise FixGuardrailError("Generator command must contain at least one non-empty argument.")
    if timeout_seconds < 1 or timeout_seconds > 1800:
        raise FixGuardrailError("Generator timeout must be between 1 and 1800 seconds.")
    source_env = environment or os.environ
    child_env = {
        name: source_env[name]
        for name in ("HOME", "PATH", "SSL_CERT_FILE", "TMPDIR")
        if source_env.get(name)
    }
    for name in pass_env:
        if "=" in name:
            raise FixGuardrailError("Generator environment inputs must be variable names, not assignments.")
        if source_env.get(name):
            child_env[name] = source_env[name]
    run = runner or _default_runner
    result = run(command, cwd.resolve(), json.dumps(request, sort_keys=True), child_env, timeout_seconds)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise FixGuardrailError(
            f"Generator exited with code {result.returncode}: {details[:2000] or 'no output'}"
        )
    output = (result.stdout or "").strip()
    try:
        raw = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FixGuardrailError("Generator output must be one JSON object with no Markdown or logs.") from exc
    if not isinstance(raw, dict):
        raise FixGuardrailError("Generator output must be one JSON object.")
    return raw


def run_generator(
    context_pack: dict[str, Any],
    *,
    command: Sequence[str],
    cwd: Path,
    pass_env: Sequence[str] = (),
    timeout_seconds: int = 300,
    runner: GeneratorRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> ChangeSet:
    gap = context_pack.get("gap") or {}
    gap_id = gap.get("id")
    if not isinstance(gap_id, str) or not gap_id:
        raise FixGuardrailError("Context pack has no selected gap ID.")
    context_sha256 = canonical_sha256(context_pack)
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "change-set.schema.json"
    request = {
        "schema_version": "0.1.0",
        "task": "Produce the smallest provider-owned change set that addresses the selected gap.",
        "context_pack_sha256": context_sha256,
        "rules": [
            "Return one JSON object and no Markdown or commentary.",
            "Use only create or replace operations allowed by the output schema.",
            "Do not include credentials, edit validation-suite contracts, or invent scope.",
            "Every content_sha256 must be the SHA-256 of the UTF-8 content field.",
            "Prefer the smallest change and preserve cleanup/error behavior required by the contract.",
        ],
        "output_schema": json.loads(schema_path.read_text(encoding="utf-8")),
        "context_pack": context_pack,
    }
    raw = dispatch_generator(
        request,
        command=command,
        cwd=cwd,
        pass_env=pass_env,
        timeout_seconds=timeout_seconds,
        runner=runner,
        environment=environment,
    )
    validate_change_set(raw)
    if raw["gap_id"] != gap_id:
        raise FixGuardrailError(f"Generator returned gap '{raw['gap_id']}', expected '{gap_id}'.")
    if raw["context_pack_sha256"] != context_sha256:
        raise FixGuardrailError("Generator output is not bound to the supplied context pack hash.")
    return change_set_from_dict(raw)


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    request: str,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        input=request,
        env=dict(environment),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
