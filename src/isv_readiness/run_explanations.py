"""Deterministic, source-linked explanations for one completed live run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.decision import decide_gap
from isv_readiness.failure_feedback import redact_failure_text
from isv_readiness.project import ReadinessProject
from isv_readiness.runs import JUNIT_FILENAME, LOG_FILENAME, RUN_EXPLANATIONS_FILENAME
from isv_readiness.schema import load_schema

RUN_EXPLANATIONS_VERSION = "0.1.0"
_DEFAULT_REASON_CODES = {
    "pass": "validation_passed",
    "fail": "validation_failed",
    "not_implemented": "not_implemented",
    "skipped": "runtime_skip",
    "error": "validation_error",
}
_SCOPE_EXCLUSION_ACTIONS = {"skip_with_rationale", "request_external_adapter"}


class RunExplanationError(ValueError):
    """Raised when a completed live run cannot produce a valid explanation artifact."""


def write_run_explanations(
    project: ReadinessProject,
    manifest_path: Path,
    *,
    run_dir: Path,
    run_id: str,
    domain: str,
    exit_code: int | None,
    success: bool,
    report: Mapping[str, Any],
) -> Path:
    """Write explanations derived from JUnit rows and the reviewed scope.

    Dynamic rows describe checks that actually ran. Reviewed scope exclusions
    are added only when no matching dynamic testcase exists. No model is called
    and no explanation is inferred from logs beyond the redacted scanner fields.
    """

    profile_sha256 = None
    if project.assessment.profile:
        profile_path = project.resolve_path(manifest_path, project.assessment.profile)
        if profile_path.is_file():
            profile_sha256 = _file_sha256(profile_path)

    checks = _check_explanations(report, domain)
    payload = {
        "schema_version": RUN_EXPLANATIONS_VERSION,
        "run": {
            "run_id": run_id,
            "provider": project.provider.name,
            "domain": domain,
            "validation_commit": project.validation.resolved_commit,
            "solution_profile_sha256": profile_sha256,
            "source_report_sha256": _canonical_sha256(report),
            "exit_code": exit_code,
            "success": success,
        },
        "artifacts": {
            "junit": _artifact(run_dir / JUNIT_FILENAME),
            "log": _artifact(run_dir / LOG_FILENAME),
        },
        "checks": checks,
    }
    try:
        jsonschema.validate(payload, load_schema("run-explanations.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "run_explanations"
        raise RunExplanationError(f"Invalid run explanation artifact at {location}: {exc.message}") from exc

    output = run_dir / RUN_EXPLANATIONS_FILENAME
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _check_explanations(report: Mapping[str, Any], domain: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in report.get("rows", [])
        if isinstance(row, Mapping) and row.get("domain") == domain
    ]
    dynamic = [row for row in rows if row.get("detection") == "dynamic"]
    dynamic_keys = {_identity(row) for row in dynamic}
    scope_exclusions: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in rows:
        if row.get("detection") == "dynamic" or _identity(row) in dynamic_keys:
            continue
        profile = _profile(row)
        if profile.get("action") not in _SCOPE_EXCLUSION_ACTIONS:
            continue
        scope_exclusions.setdefault(_identity(row), row)

    records = [_record(row, source="junit") for row in dynamic]
    records.extend(_record(row, source="reviewed_scope") for row in scope_exclusions.values())
    return sorted(
        records,
        key=lambda item: (
            item["step_name"],
            item["validation_class"] or "",
            item["junit_testcase"] or "",
            item["check_id"],
        ),
    )


def _record(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    profile = _profile(row)
    decision = decide_gap(row)
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    enrichment = row.get("enrichment") if isinstance(row.get("enrichment"), Mapping) else {}
    source_status = str(row.get("status") or "error")
    outcome = "skipped" if source == "reviewed_scope" else source_status
    if outcome not in _DEFAULT_REASON_CODES:
        outcome = "error"

    junit_reason = enrichment.get("junit_reason")
    if source == "reviewed_scope":
        reason_code = (
            "approved_external_adapter"
            if profile.get("action") == "request_external_adapter"
            else "approved_scope_exclusion"
        )
        explanation = str(profile.get("rationale") or "Excluded by the reviewed solution profile.")
        validation_message = None
        junit_testcase = None
    else:
        reason_code = str(junit_reason).strip() if isinstance(junit_reason, str) and junit_reason.strip() else _DEFAULT_REASON_CODES[outcome]
        validation_message = _optional_redacted(evidence.get("validation_message"))
        explanation = validation_message or _optional_redacted(evidence.get("message")) or decision.reason
        junit_testcase = _optional_redacted(enrichment.get("junit_testcase"))

    return {
        "check_id": str(row.get("id") or "<unknown-check>"),
        "domain": str(row.get("domain") or "<unknown-domain>"),
        "step_name": str(row.get("step_name") or "<unknown-step>"),
        "validation_class": _optional_string(row.get("validation_class")),
        "requirement_id": _optional_string(row.get("requirement_id")),
        "outcome": outcome,
        "source": source,
        "reason_code": redact_failure_text(reason_code),
        "explanation": redact_failure_text(explanation),
        "validation_message": validation_message,
        "junit_testcase": junit_testcase,
        "scope": {
            "matched": profile.get("matched") is True,
            "owned": profile.get("owned") if isinstance(profile.get("owned"), bool) else None,
            "capability_id": _optional_string(profile.get("capability_id")),
            "coverage": _optional_string(profile.get("coverage")),
            "validation_mode": _optional_string(profile.get("validation_mode")),
            "action": str(profile.get("action") or decision.action),
            "rationale": _optional_redacted(profile.get("rationale")),
            "evidence_refs": [
                str(item)
                for item in profile.get("evidence_refs", [])
                if isinstance(item, str)
            ],
        },
        "decision": {
            "blocking": decision.blocking,
            "edit_eligible": decision.edit_eligible,
            "action": decision.action,
            "reason": decision.reason,
        },
    }


def _profile(row: Mapping[str, Any]) -> Mapping[str, Any]:
    enrichment = row.get("enrichment")
    if not isinstance(enrichment, Mapping):
        return {}
    profile = enrichment.get("solution_profile")
    return profile if isinstance(profile, Mapping) else {}


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("step_name") or ""),
        str(row.get("validation_class") or ""),
        str(row.get("requirement_id") or ""),
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_redacted(value: Any) -> str | None:
    return redact_failure_text(value) if isinstance(value, str) and value else None


def _artifact(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    return {"path": path.name, "sha256": _file_sha256(path)}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
