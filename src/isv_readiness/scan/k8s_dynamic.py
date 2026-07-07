from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from isv_readiness.scan.k8s_scope import K8sScope, classify_k8s_gap
from isv_readiness.scan.models import Evidence, GapRow, Remediation, Status

_VALIDATION_RE = re.compile(r"\[([^\]]+)\]")
_STEP_NOT_CONFIGURED_RE = re.compile(r"step '([^']+)' is not configured")


@dataclass(frozen=True)
class K8sDynamicArtifacts:
    provider_repo: Path
    junit_path: Path | None = None
    log_path: Path | None = None
    setup_json_path: Path | None = None
    config_path: Path | None = None
    scope: K8sScope = field(default_factory=K8sScope)


def scan_k8s_artifacts(options: K8sDynamicArtifacts) -> list[GapRow]:
    rows: list[GapRow] = []
    log_text = _read_optional_text(options.log_path)

    if options.setup_json_path is not None:
        setup_row = _scan_setup_inventory(options)
        if setup_row is not None:
            rows.append(setup_row)

    if options.junit_path is not None:
        rows.extend(_scan_junit(options, log_text))

    return sorted(rows, key=lambda row: (row.domain, row.step_name, row.validation_class or "", row.id))


def _scan_setup_inventory(options: K8sDynamicArtifacts) -> GapRow | None:
    if options.setup_json_path is None:
        return None
    try:
        data = json.loads(options.setup_json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _dynamic_row(
            options,
            step_name="setup",
            validation_class="StepOutputSchema",
            status="error",
            message=f"Failed to parse setup inventory JSON: {exc}",
            validation_message=None,
            target=None,
            auto_fixable=False,
            layer="node_inventory",
            classification_note="Setup inventory must be valid JSON before dynamic validation can be trusted.",
        )

    missing = [field for field in ("success", "platform", "kubernetes") if field not in data]
    success = data.get("success") is True and data.get("platform") == "kubernetes" and not missing
    if success:
        return _dynamic_row(
            options,
            step_name="setup",
            validation_class="StepOutputSchema",
            status="pass",
            message="Setup inventory JSON was captured and has the required K8s envelope fields.",
            validation_message=None,
            target=None,
            auto_fixable=False,
            layer="node_inventory",
            classification_note="Setup inventory is usable as dynamic validation context.",
        )

    details = []
    if missing:
        details.append("missing fields: " + ", ".join(missing))
    if data.get("success") is not True:
        details.append("success is not true")
    if data.get("platform") != "kubernetes":
        details.append("platform is not kubernetes")
    return _dynamic_row(
        options,
        step_name="setup",
        validation_class="StepOutputSchema",
        status="fail",
        message="Setup inventory JSON is not ready for K8s validation: " + "; ".join(details),
        validation_message=None,
        target=None,
        auto_fixable=False,
        layer="node_inventory",
        classification_note="The setup script needs to emit the K8s inventory envelope expected by isvctl.",
    )


def _scan_junit(options: K8sDynamicArtifacts, log_text: str | None) -> list[GapRow]:
    if options.junit_path is None:
        return []
    tree = ET.parse(options.junit_path)
    rows: list[GapRow] = []
    seen: set[tuple[str, str | None, Status]] = set()
    for case in tree.iter("testcase"):
        validation_class = _validation_class_from_case(case)
        status, message, junit_reason = _status_message_and_reason(case)
        step_name = _step_for_validation(validation_class, message)
        key = (step_name, validation_class, status)
        if key in seen:
            continue
        seen.add(key)
        classification = classify_k8s_gap(validation_class, status, message, options.scope)
        rows.append(
            _dynamic_row(
                options,
                step_name=step_name,
                validation_class=validation_class,
                status=status,
                message=_summary_message(status, validation_class, message),
                validation_message=message or None,
                target=_target_for_classification(options, classification.auto_fixable),
                auto_fixable=classification.auto_fixable,
                layer=classification.layer,
                classification_note=classification.note,
                stderr_excerpt=_log_excerpt(log_text, validation_class),
                junit_reason=junit_reason,
            )
        )
    return rows


def _validation_class_from_case(case: ET.Element) -> str | None:
    name = case.get("name") or ""
    match = _VALIDATION_RE.search(name)
    if match:
        return match.group(1)
    if "::" in name:
        return name.split("::", 1)[0]
    return name or None


def _status_message_and_reason(case: ET.Element) -> tuple[Status, str, str | None]:
    failure = case.find("failure")
    if failure is not None:
        return "fail", _element_message(failure), failure.get("type")
    error = case.find("error")
    if error is not None:
        return "error", _element_message(error), error.get("type")
    skipped = case.find("skipped")
    if skipped is not None:
        message = _element_message(skipped)
        reason = skipped.get("type") or message.split(":", 1)[0].strip()
        return "skipped", message, reason or None
    return "pass", "Validation passed.", None


def _element_message(element: ET.Element) -> str:
    message = element.get("message") or ""
    text = (element.text or "").strip()
    if message and text and text not in message:
        return f"{message}\n{text}"
    return message or text


def _step_for_validation(validation_class: str | None, message: str) -> str:
    match = _STEP_NOT_CONFIGURED_RE.search(message)
    if match:
        return match.group(1)
    if validation_class is None:
        return "<validation>"
    if validation_class.startswith("K8sNodePool"):
        return "node_pool"
    if validation_class.startswith(("K8sGpu", "K8sNvidia", "K8sDriver", "K8sMig")):
        return "setup"
    if validation_class.startswith("K8sNetworkPolicy"):
        return "network_policy"
    if validation_class.startswith("K8sCsi"):
        return "storage_csi"
    if validation_class.startswith("K8sOidc"):
        return "identity_oidc"
    if validation_class.startswith(("K8sApiServerMetrics", "K8sControlPlaneLogs")):
        return "observability"
    if validation_class.startswith(("K8sNccl", "K8sGpuStress", "K8sNim")):
        return "workloads"
    if validation_class.startswith("K8sApiNetworkAcl"):
        return "api_network_acl"
    return "setup"


def _summary_message(status: Status, validation_class: str | None, message: str) -> str:
    label = validation_class or "validation"
    if status == "pass":
        return f"{label} passed in the dynamic K8s run."
    if status == "skipped":
        return f"{label} was skipped in the dynamic K8s run: {message}"
    return f"{label} {status}ed in the dynamic K8s run: {message}"


def _target_for_classification(options: K8sDynamicArtifacts, auto_fixable: bool) -> str | None:
    if not auto_fixable:
        return None
    if options.config_path is not None:
        return _relative_or_str(options.config_path, options.provider_repo)
    return None


def _dynamic_row(
    options: K8sDynamicArtifacts,
    *,
    step_name: str,
    validation_class: str | None,
    status: Status,
    message: str,
    validation_message: str | None,
    target: str | None,
    auto_fixable: bool,
    layer: str | None,
    classification_note: str,
    stderr_excerpt: str | None = None,
    junit_reason: str | None = None,
) -> GapRow:
    spine = "|".join(["k8s", step_name, validation_class or "", "", "dynamic"])
    return GapRow(
        id="gap_" + hashlib.sha1(spine.encode("utf-8")).hexdigest()[:12],
        domain="k8s",
        step_name=step_name,
        validation_class=validation_class,
        requirement_id=None,
        milestone=None,
        status=status,
        detection="dynamic",
        stage="correctness" if status in {"fail", "error"} else "coverage",
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
        enrichment={
            **options.scope.to_enrichment(layer, classification_note),
            "junit_reason": junit_reason,
        },
    )


def _rerun_command(options: K8sDynamicArtifacts) -> str:
    if options.config_path is None:
        return "isvctl test run -f <k8s-provider-config>"
    return f"isvctl test run -f {_relative_or_str(options.config_path, options.provider_repo)}"


def _read_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _log_excerpt(log_text: str | None, validation_class: str | None, context_lines: int = 3) -> str | None:
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
