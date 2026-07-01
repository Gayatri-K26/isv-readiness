from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from isv_readiness.scan.models import Evidence, GapRow, GapType, Remediation, Status

_VALIDATION_RE = re.compile(r"\[([^\]]+)\]")
_STEP_NOT_CONFIGURED_RE = re.compile(r"step '([^']+)' is not configured")

_STRUCTURED_REASONS = {
    "invalid_config",
    "phase_not_requested",
    "runtime_exception",
    "runtime_skip",
    "step_no_output",
    "step_not_configured",
    "template_render_failed",
    "test_excluded",
    "unreleased",
}

_LAB_ENV_MARKERS = (
    "credential",
    "environment variable",
    "missing dependency",
    "not reachable",
    "permission denied",
    "timed out",
    "timeout",
)


@dataclass(frozen=True)
class DynamicArtifacts:
    provider_repo: Path
    domain: str
    junit_path: Path
    log_path: Path | None = None
    config_path: Path | None = None
    static_rows: tuple[GapRow, ...] = ()


def scan_dynamic_artifacts(options: DynamicArtifacts) -> list[GapRow]:
    log_text = _read_optional_text(options.log_path)
    try:
        tree = ET.parse(options.junit_path)
    except (OSError, ET.ParseError) as exc:
        return [
            _dynamic_row(
                options,
                testcase_name="<junit>",
                step_name="<dynamic-run>",
                validation_class="JUnitContract",
                validation_category=None,
                status="error",
                gap_type="lab_env",
                message=f"Failed to parse JUnit artifact: {exc}",
                validation_message=str(exc),
                reason="invalid_junit",
                auto_fixable=False,
                stderr_excerpt=None,
            )
        ]

    rows: list[GapRow] = []
    for case in tree.iter("testcase"):
        testcase_name = case.get("name") or "<unnamed-testcase>"
        validation_class = _validation_class_from_case(case)
        status, message, reason = _status_message_and_reason(case)
        step_name, validation_category = _validation_context(
            validation_class,
            message,
            options.static_rows,
        )
        gap_type, auto_fixable, classification_note = _classify(status, message, reason)
        rows.append(
            _dynamic_row(
                options,
                testcase_name=testcase_name,
                step_name=step_name,
                validation_class=validation_class,
                validation_category=validation_category,
                status=status,
                gap_type=gap_type,
                message=_summary_message(status, validation_class, message),
                validation_message=message or None,
                reason=reason,
                auto_fixable=auto_fixable,
                stderr_excerpt=_log_excerpt(log_text, validation_class),
                classification_note=classification_note,
            )
        )
    return sorted(
        rows,
        key=lambda row: (row.domain, row.step_name, row.validation_class or "", row.id),
    )


def _validation_class_from_case(case: ET.Element) -> str | None:
    name = case.get("name") or ""
    match = _VALIDATION_RE.search(name)
    if match:
        return match.group(1)
    if "::" in name:
        return name.split("::", 1)[-1]
    return name or None


def _status_message_and_reason(case: ET.Element) -> tuple[Status, str, str | None]:
    for element_name, status in (("failure", "fail"), ("error", "error"), ("skipped", "skipped")):
        element = case.find(element_name)
        if element is not None:
            message = _element_message(element)
            return status, message, _reason_code(element, message)
    return "pass", "Validation passed.", None


def _element_message(element: ET.Element) -> str:
    message = element.get("message") or ""
    text = (element.text or "").strip()
    if message and text and text not in message:
        return f"{message}\n{text}"
    return message or text


def _reason_code(element: ET.Element, message: str) -> str | None:
    raw_type = element.get("type")
    if raw_type:
        return raw_type
    prefix = message.split(":", 1)[0].strip().lower()
    return prefix if prefix in _STRUCTURED_REASONS else None


def _validation_context(
    validation_class: str | None,
    message: str,
    static_rows: tuple[GapRow, ...],
) -> tuple[str, str | None]:
    message_step = _STEP_NOT_CONFIGURED_RE.search(message)
    candidates = [
        row
        for row in static_rows
        if _same_validation(validation_class, row.validation_class)
    ]
    if message_step:
        step_name = message_step.group(1)
        candidates_for_step = [row for row in candidates if row.step_name == step_name]
        return step_name, _unique_category(candidates_for_step or candidates)

    steps = {row.step_name for row in candidates}
    step_name = next(iter(steps)) if len(steps) == 1 else "<validation>"
    return step_name, _unique_category(candidates)


def _same_validation(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return left == right
    return left == right or left.startswith(f"{right}-") or right.startswith(f"{left}-")


def _unique_category(rows: list[GapRow]) -> str | None:
    categories = {
        category
        for row in rows
        if isinstance((category := row.enrichment.get("validation_category")), str)
    }
    return next(iter(categories)) if len(categories) == 1 else None


def _classify(status: Status, message: str, reason: str | None) -> tuple[GapType, bool, str]:
    if status == "pass":
        return "provider_script", False, "Validation passed; no remediation route is needed."
    if reason == "step_not_configured":
        return "onboarding", True, "The provider config does not declare the validation's required step."
    if reason == "step_no_output":
        return "provider_script", True, "The configured provider step did not produce JSON output."
    if reason in {"invalid_config", "template_render_failed"}:
        return "provider_script", True, "Provider configuration could not be resolved for validation."
    if reason in {"test_excluded", "unreleased"}:
        return "semantic_mismatch", False, "The validation was intentionally excluded from this run."
    if reason == "phase_not_requested":
        return "lab_env", False, "The validation phase was not requested in this run."
    if reason == "runtime_exception":
        return "provider_script", False, "Runtime failure requires triage before an edit can be selected."
    if status == "skipped" or any(marker in message.lower() for marker in _LAB_ENV_MARKERS):
        return "lab_env", False, "Runtime prerequisites or environment state prevented validation."
    return "provider_script", False, "Validation failed; classify against solution ownership before editing."


def _summary_message(status: Status, validation_class: str | None, message: str) -> str:
    label = validation_class or "validation"
    if status == "pass":
        return f"{label} passed in the dynamic run."
    if status == "skipped":
        return f"{label} was skipped in the dynamic run: {message}"
    return f"{label} {status}ed in the dynamic run: {message}"


def _dynamic_row(
    options: DynamicArtifacts,
    *,
    testcase_name: str,
    step_name: str,
    validation_class: str | None,
    validation_category: str | None,
    status: Status,
    gap_type: GapType,
    message: str,
    validation_message: str | None,
    reason: str | None,
    auto_fixable: bool,
    stderr_excerpt: str | None,
    classification_note: str = "JUnit artifact could not be consumed.",
) -> GapRow:
    spine = "|".join(
        [options.domain, step_name, validation_class or "", "", "dynamic", testcase_name]
    )
    target = _relative_or_str(options.config_path, options.provider_repo) if auto_fixable else None
    enrichment = {
        "junit_testcase": testcase_name,
        "junit_reason": reason,
        "classification_note": classification_note,
    }
    if validation_category is not None:
        enrichment["validation_category"] = validation_category
    return GapRow(
        id="gap_" + hashlib.sha1(spine.encode("utf-8")).hexdigest()[:12],
        domain=options.domain,
        step_name=step_name,
        validation_class=validation_class,
        requirement_id=None,
        milestone=None,
        status=status,
        detection="dynamic",
        stage="correctness" if status in {"fail", "error"} else "coverage",
        gap_type=gap_type,
        evidence=Evidence(
            message=message,
            validation_message=validation_message,
            schema_errors=[],
            missing_json_fields=[],
            stderr_excerpt=stderr_excerpt,
            script_path=None,
            config_path=_relative_or_str(options.config_path, options.provider_repo),
        ),
        remediation=Remediation(
            auto_fixable=auto_fixable,
            target=target,
            rerun_command=_rerun_command(options),
            aws_reference=None,
        ),
        enrichment=enrichment,
    )


def _rerun_command(options: DynamicArtifacts) -> str:
    if options.config_path is None:
        return f"isvctl test run -f <{options.domain}-provider-config>"
    return f"isvctl test run -f {_relative_or_str(options.config_path, options.provider_repo)}"


def _read_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _log_excerpt(
    log_text: str | None,
    validation_class: str | None,
    context_lines: int = 3,
) -> str | None:
    if not log_text or not validation_class:
        return None
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if validation_class in line:
            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            return "\n".join(lines[start:end])[:2000]
    return None


def _relative_or_str(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
