from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from isv_readiness.changes import ChangeSet, canonical_sha256, change_set_from_dict, validate_change_set
from isv_readiness.context import (
    LIFECYCLE_TIMEOUT_CONSTRAINT,
    PROVIDER_IMPLEMENTATION_RULES,
    provider_contract_constraints,
)
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.project import MINIMAL_PROCESS_ENV
from isv_readiness.schema import load_schema
from isv_readiness.subprocesses import run_captured

DEFAULT_GENERATOR_TIMEOUT_SECONDS = 900
GENERATOR_PROCESS_ENV = (*MINIMAL_PROCESS_ENV, "USER")

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
    timeout_seconds: int = DEFAULT_GENERATOR_TIMEOUT_SECONDS,
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
    source_env = environment if environment is not None else os.environ
    child_env = {
        name: source_env[name]
        for name in GENERATOR_PROCESS_ENV
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
        if len(details) > 2000:
            details = "..." + details[-2000:]
        raise FixGuardrailError(f"Generator exited with code {result.returncode}: {details or 'no output'}")
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
    timeout_seconds: int = DEFAULT_GENERATOR_TIMEOUT_SECONDS,
    runner: GeneratorRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> ChangeSet:
    gap = context_pack.get("gap") or {}
    gap_id = gap.get("id")
    if not isinstance(gap_id, str) or not gap_id:
        raise FixGuardrailError("Context pack has no selected gap ID.")
    context_sha256 = canonical_sha256(context_pack)
    contract_constraints = provider_contract_constraints(context_pack)
    source_rules: list[str] = []
    lifecycle_timeout = contract_constraints.get(LIFECYCLE_TIMEOUT_CONSTRAINT)
    if lifecycle_timeout is not None:
        source_rules.append(
            "The authoritative provider contract sets lifecycle_step_timeout_seconds="
            f"{lifecycle_timeout:g}. For lifecycle adapters, keep the explicit internal recovery deadline at or "
            "above that value and set the configured step timeout to contain it; bounded runner headroom is allowed."
        )
    request = {
        "schema_version": "0.1.0",
        "task": (
            "Produce the smallest source-grounded provider-owned change set that addresses the selected gap "
            "and the shared adapter contract represented by any related_target_gaps. Implement the reviewed "
            "capability mapping with a bounded adapter that fails closed on unsupported runtime data. Return an "
            "empty changes array only when a required provider interface is absent or the pinned validation "
            "contract is structurally incompatible with the allowed edit boundary."
        ),
        "context_pack_sha256": context_sha256,
        "rules": [
            "Return one JSON object and no Markdown or commentary.",
            "Use only create or replace operations allowed by the output schema.",
            "Do not include credentials, edit validation-suite contracts, or invent scope.",
            "Every content_sha256 must be the SHA-256 of the UTF-8 content field.",
            "Prefer the smallest change and preserve cleanup/error behavior required by the contract.",
            (
                "Every non-empty change set must include the selected remediation target. It may also include "
                "other provider-owned scripts and the selected domain configuration when those edits are required "
                "by the same adapter contract; do not assume the selected target is the only authorized file."
            ),
            (
                "Refuse only for an absent required interface or a structural contract mismatch. Unverified runtime "
                "behavior should be tested by fail-closed adapter code, not treated as proof of impossibility. Never "
                "fabricate a passing result."
            ),
            *PROVIDER_IMPLEMENTATION_RULES,
            *source_rules,
        ],
        "output_schema": load_schema("change-set.schema.json"),
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
    return run_captured(
        command,
        cwd=cwd,
        input_text=request,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
