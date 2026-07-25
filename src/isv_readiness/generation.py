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
from isv_readiness.generator_limits import (
    GENERATOR_ADAPTER_TIMEOUT_SECONDS,
    MAX_GENERATOR_REQUEST_BYTES,
    MAX_GENERATOR_TIMEOUT_SECONDS,
)
from isv_readiness.generators import DEFAULT_GENERATOR_MAX_REQUEST_BYTES
from isv_readiness.project import MINIMAL_PROCESS_ENV
from isv_readiness.schema import load_schema
from isv_readiness.subprocesses import CapturedIdleTimeout, run_captured

DEFAULT_GENERATOR_TIMEOUT_SECONDS = GENERATOR_ADAPTER_TIMEOUT_SECONDS
GENERATOR_PROCESS_ENV = (*MINIMAL_PROCESS_ENV, "USER")

GeneratorRunner = Callable[
    [Sequence[str], Path, str, Mapping[str, str], int],
    subprocess.CompletedProcess[str],
]


class GeneratorInfrastructureError(FixGuardrailError):
    """Raised when the configured generator cannot complete a model call."""


def dispatch_generator(
    request: dict[str, Any],
    *,
    command: Sequence[str],
    cwd: Path,
    pass_env: Sequence[str] = (),
    timeout_seconds: int = DEFAULT_GENERATOR_TIMEOUT_SECONDS,
    idle_timeout_seconds: int | None = None,
    max_request_bytes: int = DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
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
    if timeout_seconds < 1 or timeout_seconds > MAX_GENERATOR_TIMEOUT_SECONDS:
        raise FixGuardrailError(f"Generator timeout must be between 1 and {MAX_GENERATOR_TIMEOUT_SECONDS} seconds.")
    if idle_timeout_seconds is not None and (
        idle_timeout_seconds < 1 or idle_timeout_seconds > timeout_seconds
    ):
        raise FixGuardrailError("Generator idle timeout must be positive and no greater than its total timeout.")
    if max_request_bytes < 1 or max_request_bytes > MAX_GENERATOR_REQUEST_BYTES:
        raise FixGuardrailError(
            f"Generator max request size must be between 1 and {MAX_GENERATOR_REQUEST_BYTES} bytes."
        )
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
    serialized_request = json.dumps(request, sort_keys=True)
    request_bytes = len(serialized_request.encode("utf-8"))
    if request_bytes > max_request_bytes:
        raise GeneratorInfrastructureError(
            f"Complete generator request is {request_bytes} bytes, exceeding this adapter's "
            f"{max_request_bytes}-byte capability. Select an adapter with a larger context capacity; "
            "the request was not truncated."
        )
    try:
        if runner is None:
            result = _default_runner(
                command,
                cwd.resolve(),
                serialized_request,
                child_env,
                timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        else:
            result = runner(command, cwd.resolve(), serialized_request, child_env, timeout_seconds)
    except CapturedIdleTimeout as exc:
        raise GeneratorInfrastructureError(
            f"Generator adapter produced no output for {idle_timeout_seconds} seconds."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GeneratorInfrastructureError(
            f"Generator adapter timed out after {timeout_seconds} seconds."
        ) from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 2000:
            details = "..." + details[-2000:]
        if result.returncode == 124:
            raise GeneratorInfrastructureError(
                f"Generator model timed out: {details or 'no diagnostic output'}"
            )
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
    idle_timeout_seconds: int | None = None,
    max_request_bytes: int = DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
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
        idle_timeout_seconds=idle_timeout_seconds,
        max_request_bytes=max_request_bytes,
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
    *,
    idle_timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_captured(
        command,
        cwd=cwd,
        input_text=request,
        environment=environment,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
