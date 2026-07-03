from __future__ import annotations

from dataclasses import replace
from typing import Any

from isv_readiness.scan.models import GapReport, GapRow
from isv_readiness.solution_profile import SolutionProfile, canonicalize_domain

# A deterministic test result outranks an operator's ownership claim. When the
# scan says a required check on an ISV-owned domain is broken, the profile is not
# allowed to silently drop it: these dispositions assert "someone else or nobody
# needs to act", which contradicts a failing owned check and is treated as a
# misidentification rather than obeyed.
FAILURE_STATUSES = {"fail", "not_implemented", "error"}
SILENT_SKIP_ACTIONS = {"skip_with_rationale", "request_external_adapter"}


def enrich_report_with_profile(report: GapReport, profile: SolutionProfile) -> GapReport:
    """Add deterministic responsibility routing without changing validation outcomes."""
    rows = [_enrich_row(row, profile) for row in report.rows]
    return GapReport(
        schema_version=report.schema_version,
        provider_repo=report.provider_repo,
        domains=report.domains,
        isv_context=report.isv_context,
        rows=rows,
    )


def _enrich_row(row: GapRow, profile: SolutionProfile) -> GapRow:
    validation_category = row.enrichment.get("validation_category")
    responsibility = profile.resolve(
        row.domain,
        step_name=row.step_name,
        validation_category=(
            validation_category if isinstance(validation_category, str) else None
        ),
        validation_class=row.validation_class,
    )
    owned = _domain_owned(profile, row.domain)

    if responsibility is None:
        profile_enrichment: dict[str, Any] = {
            "profile_id": profile.solution.id,
            "matched": False,
            "owned": owned,
            "action": "request_scope_decision",
            "reason": f"Domain '{row.domain}' is not declared in the solution profile.",
        }
        allow_auto_fix = False
    else:
        profile_enrichment = {
            "profile_id": profile.solution.id,
            "matched": True,
            "owned": owned,
            "capability_id": responsibility.capability_id,
            "coverage": responsibility.coverage,
            "validation_mode": responsibility.validation_mode,
            "capability_owner_actor_id": responsibility.capability_owner_actor_id,
            "provider_adapter_owner_actor_id": responsibility.provider_adapter_owner_actor_id,
            "component_ids": list(responsibility.component_ids),
            "action": responsibility.action,
            "rationale": responsibility.rationale,
            "required_inputs": list(responsibility.required_inputs),
            "evidence_refs": list(responsibility.evidence_refs),
        }
        allow_auto_fix = responsibility.action == "implement_or_fix_adapter"

    _reconcile_masked_failure(row, profile_enrichment, owned=owned)

    enrichment = dict(row.enrichment)
    enrichment["solution_profile"] = profile_enrichment
    remediation = replace(
        row.remediation,
        auto_fixable=row.remediation.auto_fixable and allow_auto_fix,
    )
    return replace(row, remediation=remediation, enrichment=enrichment)


def _reconcile_masked_failure(
    row: GapRow, profile_enrichment: dict[str, Any], *, owned: bool
) -> None:
    """Refuse to silently skip a failing required check on an ISV-owned domain.

    The profile is operator-asserted and can be wrong: an ISV can mark a
    capability partner/lab-owned that is really its own responsibility. When that
    disposition would drop a check the deterministic scan reports as broken, the
    test result wins and the row is re-routed to an explicit scope decision
    instead of being masked as skipped/external.
    """
    if not owned or row.status not in FAILURE_STATUSES:
        return
    if profile_enrichment.get("action") not in SILENT_SKIP_ACTIONS:
        return
    profile_enrichment["reconciliation"] = {
        "masked_failure": True,
        "original_action": profile_enrichment["action"],
        "detail": (
            f"Scan status '{row.status}' contradicts disposition "
            f"'{profile_enrichment['action']}' on ISV-owned domain '{row.domain}'. "
            "The validation result outranks the ownership claim; confirm ownership "
            "before skipping."
        ),
    }
    profile_enrichment["action"] = "request_scope_decision"


def _domain_owned(profile: SolutionProfile, domain: str) -> bool:
    canonical = canonicalize_domain(domain)
    scope = next((item for item in profile.domains if item.domain == canonical), None)
    return bool(scope and scope.owned)
