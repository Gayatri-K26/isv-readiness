from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import jsonschema

from isv_readiness.agent_skill import with_agent_skill
from isv_readiness.changes import canonical_sha256
from isv_readiness.context import build_context_pack
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.generation import GeneratorRunner, dispatch_generator
from isv_readiness.generators import DEFAULT_GENERATOR_MAX_REQUEST_BYTES
from isv_readiness.onboarding import DOMAIN_CONFIG_FILES
from isv_readiness.project import ReadinessProject
from isv_readiness.scan.models import Evidence, GapRow, Remediation
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import DomainResponsibility, SolutionProfile

DOMAIN_AUDIT_VERSION = "0.1.0"
AuditStatus = Literal["implemented", "gap", "scope_question"]


class DomainAuditError(FixGuardrailError):
    """Raised when a semantic domain audit is incomplete or ungrounded."""


@dataclass(frozen=True)
class ApprovedCapability:
    capability_id: str
    name: str
    selectors: dict[str, list[str]]
    rationale: str
    required_inputs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    component_ids: tuple[str, ...]
    capability_owner_actor_id: str
    provider_adapter_owner_actor_id: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("required_inputs", "evidence_refs", "component_ids"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class AuditEvidence:
    effect: str
    path: str
    detail: str


@dataclass(frozen=True)
class CapabilityAudit:
    capability_id: str
    status: AuditStatus
    step_name: str
    target: str | None
    expected_effects: dict[str, tuple[str, ...]]
    implementation_evidence: tuple[AuditEvidence, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expected_effects"] = {
            phase: list(effects) for phase, effects in self.expected_effects.items()
        }
        payload["implementation_evidence"] = [asdict(item) for item in self.implementation_evidence]
        return payload


@dataclass(frozen=True)
class DomainAudit:
    schema_version: str
    domain: str
    audit_context_sha256: str
    auditor: dict[str, Any]
    summary: str
    capabilities: tuple[CapabilityAudit, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["capabilities"] = [item.to_dict() for item in self.capabilities]
        return payload


def approved_test_capabilities(profile: SolutionProfile, domain: str) -> tuple[ApprovedCapability, ...]:
    scope = next((item for item in profile.domains if item.domain == domain), None)
    if scope is None or not scope.owned:
        return ()
    actors = {actor.id: actor for actor in profile.actors}
    approved: list[ApprovedCapability] = []
    for capability in scope.capabilities:
        coverage = capability.coverage or scope.coverage
        mode = capability.validation_mode or scope.validation_mode
        if coverage != "covered" or mode != "test":
            continue
        adapter_owner = capability.provider_adapter_owner_actor_id or scope.provider_adapter_owner_actor_id
        approved.append(
            ApprovedCapability(
                capability_id=capability.id,
                name=capability.name,
                selectors={
                    "steps": list(capability.selector.steps),
                    "validation_categories": list(capability.selector.validation_categories),
                    "validation_classes": list(capability.selector.validation_classes),
                },
                rationale=capability.rationale or scope.rationale,
                required_inputs=capability.required_inputs or scope.required_inputs,
                evidence_refs=capability.evidence_refs or scope.evidence_refs,
                component_ids=capability.component_ids or scope.component_ids,
                capability_owner_actor_id=(
                    capability.capability_owner_actor_id or scope.capability_owner_actor_id
                ),
                provider_adapter_owner_actor_id=adapter_owner,
                action=(
                    "implement_or_fix_adapter"
                    if actors[adapter_owner].kind == "isv"
                    else "request_external_adapter"
                ),
            )
        )
    if approved:
        return tuple(approved)
    if scope.coverage != "covered" or scope.validation_mode != "test":
        return ()
    return (_default_capability(scope, actors[scope.provider_adapter_owner_actor_id].kind),)


def run_domain_audit(
    project: ReadinessProject,
    manifest_path: Path,
    report: dict[str, Any],
    profile: SolutionProfile,
    *,
    domain: str,
    provider_repo: Path,
    work_dir: Path,
    command: Sequence[str],
    pass_env: Sequence[str],
    environment: Mapping[str, str] | None,
    runner: GeneratorRunner | None,
    timeout_seconds: int,
    idle_timeout_seconds: int | None,
    max_request_bytes: int = DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
) -> tuple[DomainAudit, tuple[ApprovedCapability, ...]]:
    capabilities = approved_test_capabilities(profile, domain)
    if not capabilities:
        raise DomainAuditError(f"Domain '{domain}' has no approved covered/test capability to audit.")
    anchor = _audit_anchor(report, domain)
    if anchor is None:
        raise DomainAuditError(f"Domain '{domain}' has no scanner row that can anchor a completeness audit.")
    context_pack = build_context_pack(
        project,
        manifest_path,
        report,
        gap_id=str(anchor["id"]),
        cache_dir=manifest_path.parent / ".gapctl" / "context-cache",
        environment=environment,
        whole_domain=True,
        provider_root_override=provider_repo,
    ).to_dict()
    provider_paths = _provider_paths(context_pack, provider_repo)
    editable_targets = sorted(
        source["path"]
        for source in provider_paths
        if _is_editable_audit_target(str(source["path"]), domain)
    )
    if not editable_targets:
        raise DomainAuditError(f"Domain '{domain}' has no existing provider-owned audit target.")
    audit_context = {
        "domain": domain,
        "approved_capabilities": [item.to_dict() for item in capabilities],
        "context_pack": context_pack,
        "provider_paths": provider_paths,
        "editable_targets": editable_targets,
    }
    audit_context_sha256 = canonical_sha256(audit_context)
    request = with_agent_skill(
        {
            "schema_version": DOMAIN_AUDIT_VERSION,
            "task": (
                "Independently audit the complete existing provider lifecycle for every approved capability. "
                "Return findings only; do not edit files. A capability is implemented only when code evidence "
                "performs every source-backed setup, test, and teardown effect."
            ),
            "audit_context_sha256": audit_context_sha256,
            "rules": [
                "Return one JSON object and no Markdown or commentary.",
                "Account for every approved_capabilities entry exactly once and add no other capability IDs.",
                "Use only paths listed in provider_paths as evidence; their content is in context_pack items.",
                "For status gap, select one existing path from editable_targets as the primary target.",
                "For implemented or scope_question, target must be null.",
                "Do not treat successful output, file presence, inventory, or a no-op as proof of a mutating effect.",
                "Do not invent provider ownership, operations, or lifecycle requirements absent from supplied sources.",
            ],
            "output_schema": load_schema("domain-audit.schema.json"),
            "audit_context": audit_context,
        },
        "audit",
    )
    raw = dispatch_generator(
        request,
        command=command,
        cwd=work_dir,
        pass_env=pass_env,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        max_request_bytes=max_request_bytes,
        runner=runner,
        environment=environment,
        protected_roots=(manifest_path, project.validation_root(manifest_path), provider_repo),
    )
    audit = _parse_domain_audit(
        raw,
        domain=domain,
        audit_context_sha256=audit_context_sha256,
        capabilities=capabilities,
        source_paths={str(item["path"]) for item in provider_paths},
        editable_targets=set(editable_targets),
    )
    return audit, capabilities


def audit_gap_rows(
    audit: DomainAudit,
    capabilities: Sequence[ApprovedCapability],
    *,
    profile: SolutionProfile,
    provider_repo: Path,
    report: dict[str, Any],
) -> list[GapRow]:
    approved = {item.capability_id: item for item in capabilities}
    config_name = DOMAIN_CONFIG_FILES.get(audit.domain)
    config_path = provider_repo / "config" / config_name if config_name else None
    phases = {
        str(row.get("step_name")): (row.get("enrichment") or {}).get("validation_phase")
        for row in report.get("rows", [])
        if row.get("domain") == audit.domain
    }
    rows: list[GapRow] = []
    for finding in audit.capabilities:
        if finding.status == "implemented":
            continue
        capability = approved[finding.capability_id]
        action = capability.action if finding.status == "gap" else "request_scope_decision"
        target = finding.target if finding.status == "gap" else None
        gap_id = "gap_" + hashlib.sha256(
            f"domain-audit:{audit.domain}:{finding.capability_id}".encode()
        ).hexdigest()[:12]
        profile_enrichment = {
            "profile_id": profile.solution.id,
            "profile_status": profile.solution.profile_status,
            "journey_stage": profile.journey.stage,
            "matched": True,
            "owned": True,
            "capability_id": finding.capability_id,
            "coverage": "covered",
            "validation_mode": "test",
            "capability_owner_actor_id": capability.capability_owner_actor_id,
            "provider_adapter_owner_actor_id": capability.provider_adapter_owner_actor_id,
            "component_ids": list(capability.component_ids),
            "action": action,
            "rationale": capability.rationale,
            "required_inputs": list(capability.required_inputs),
            "evidence_refs": list(capability.evidence_refs),
        }
        rows.append(
            GapRow(
                id=gap_id,
                domain=audit.domain,
                step_name=finding.step_name,
                validation_class="DomainLifecycleAudit",
                requirement_id=finding.capability_id,
                status="not_implemented" if finding.status == "gap" else "error",
                detection="semantic",
                stage="coverage",
                evidence=Evidence(
                    message=finding.reason,
                    validation_message=json.dumps(
                        {phase: list(effects) for phase, effects in finding.expected_effects.items()},
                        sort_keys=True,
                    ),
                    script_path=target,
                    config_path=str(config_path.resolve()) if config_path and config_path.is_file() else None,
                ),
                remediation=Remediation(
                    auto_fixable=bool(target and action == "implement_or_fix_adapter"),
                    target=target,
                    rerun_command=f"gapctl auto --domain {audit.domain}",
                ),
                enrichment={
                    "validation_phase": phases.get(finding.step_name),
                    "solution_profile": profile_enrichment,
                    "domain_audit": finding.to_dict(),
                },
                labels=("lifecycle", "semantic_audit"),
            )
        )
    return rows


def merge_audit_rows(report: dict[str, Any], rows: Sequence[GapRow]) -> dict[str, Any]:
    merged = dict(report)
    merged["rows"] = [*report.get("rows", []), *(row.to_dict() for row in rows)]
    return merged


def write_domain_audit(path: Path, audit: DomainAudit) -> None:
    path.write_text(json.dumps(audit.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_capability(scope: DomainResponsibility, adapter_kind: str) -> ApprovedCapability:
    return ApprovedCapability(
        capability_id=f"{scope.domain}.default",
        name=f"{scope.name} approved domain lifecycle",
        selectors={"steps": [], "validation_categories": [], "validation_classes": []},
        rationale=scope.rationale,
        required_inputs=scope.required_inputs,
        evidence_refs=scope.evidence_refs,
        component_ids=scope.component_ids,
        capability_owner_actor_id=scope.capability_owner_actor_id,
        provider_adapter_owner_actor_id=scope.provider_adapter_owner_actor_id,
        action=("implement_or_fix_adapter" if adapter_kind == "isv" else "request_external_adapter"),
    )


def _audit_anchor(report: dict[str, Any], domain: str) -> dict[str, Any] | None:
    candidates = [row for row in report.get("rows", []) if row.get("domain") == domain]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            0 if (row.get("enrichment") or {}).get("validation_phase") == "test" else 1,
            str(row.get("step_name") or ""),
            str(row.get("id") or ""),
        ),
    )


def _provider_paths(context_pack: dict[str, Any], provider_repo: Path) -> list[dict[str, str]]:
    """Map packed provider sources to stable relative paths without copying their content."""

    root = provider_repo.resolve()
    sources: dict[str, str] = {}
    for item in context_pack.get("items", []):
        if item.get("kind") not in {"provider_config", "provider_source"}:
            continue
        try:
            relative = Path(str(item["origin"])).resolve().relative_to(root).as_posix()
        except (KeyError, ValueError):
            continue
        sources.setdefault(relative, str(item["source_id"]))
    return [
        {"path": path, "source_id": source_id}
        for path, source_id in sorted(sources.items())
    ]


def _is_editable_audit_target(path: str, domain: str) -> bool:
    config_name = DOMAIN_CONFIG_FILES.get(domain)
    return path.startswith(f"scripts/{domain}/") or bool(config_name and path == f"config/{config_name}")


def _parse_domain_audit(
    raw: Any,
    *,
    domain: str,
    audit_context_sha256: str,
    capabilities: Sequence[ApprovedCapability],
    source_paths: set[str],
    editable_targets: set[str],
) -> DomainAudit:
    try:
        jsonschema.validate(raw, load_schema("domain-audit.schema.json"))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "domain_audit"
        raise DomainAuditError(f"Invalid domain audit at {location}: {exc.message}") from exc
    if raw["domain"] != domain:
        raise DomainAuditError(f"Domain audit returned '{raw['domain']}', expected '{domain}'.")
    if raw["audit_context_sha256"] != audit_context_sha256:
        raise DomainAuditError("Domain audit is not bound to the supplied context hash.")
    expected_ids = [item.capability_id for item in capabilities]
    actual_ids = [item["capability_id"] for item in raw["capabilities"]]
    if len(actual_ids) != len(set(actual_ids)):
        raise DomainAuditError("Domain audit contains duplicate capability IDs.")
    if set(actual_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(actual_ids))
        extra = sorted(set(actual_ids) - set(expected_ids))
        raise DomainAuditError(
            "Domain audit must account for every approved capability exactly once "
            f"(missing={missing}, extra={extra})."
        )
    findings: list[CapabilityAudit] = []
    for item in raw["capabilities"]:
        effects = {
            phase: tuple(item["expected_effects"][phase])
            for phase in ("setup", "test", "teardown")
        }
        if not any(effects.values()):
            raise DomainAuditError(
                f"Capability '{item['capability_id']}' has no source-backed expected effect."
            )
        target = item["target"]
        if item["status"] == "gap":
            if target not in editable_targets:
                raise DomainAuditError(
                    f"Capability '{item['capability_id']}' selected an unauthorized audit target: {target}"
                )
        elif target is not None:
            raise DomainAuditError(
                f"Capability '{item['capability_id']}' must use a null target when status is {item['status']}."
            )
        evidence = tuple(AuditEvidence(**record) for record in item["implementation_evidence"])
        invalid_evidence = sorted({record.path for record in evidence} - source_paths)
        if invalid_evidence:
            raise DomainAuditError(
                f"Capability '{item['capability_id']}' cites unavailable provider source(s): "
                + ", ".join(invalid_evidence)
            )
        if item["status"] == "implemented" and not evidence:
            raise DomainAuditError(
                f"Capability '{item['capability_id']}' claims implemented without code evidence."
            )
        findings.append(
            CapabilityAudit(
                capability_id=item["capability_id"],
                status=item["status"],
                step_name=item["step_name"],
                target=target,
                expected_effects=effects,
                implementation_evidence=evidence,
                reason=item["reason"],
            )
        )
    by_id = {item.capability_id: item for item in findings}
    return DomainAudit(
        schema_version=raw["schema_version"],
        domain=raw["domain"],
        audit_context_sha256=raw["audit_context_sha256"],
        auditor=dict(raw["auditor"]),
        summary=raw["summary"],
        capabilities=tuple(by_id[capability_id] for capability_id in expected_ids),
    )
