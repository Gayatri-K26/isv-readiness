from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from isv_readiness.solution_profile import (
    SolutionProfile,
    blocks_readiness,
    canonicalize_domain,
    profile_is_ratified,
)

FAILURE_STATUSES = frozenset({"fail", "not_implemented", "error"})
FIX_ACTION = "implement_or_fix_adapter"
SKIP_ACTION = "skip_with_rationale"


@dataclass(frozen=True)
class GapDecision:
    """One deterministic interpretation of a raw gap row."""

    blocking: bool
    edit_eligible: bool
    action: str
    reason: str


def decide_gap(row: Mapping[str, Any]) -> GapDecision:
    """Combine the observed outcome with the reviewed responsibility route.

    ``status`` remains the upstream/static observation. A skip is accepted only
    when the profile explicitly routes that row to ``skip_with_rationale``;
    otherwise it remains visible as a blocker.
    """

    status = str(row.get("status") or "error")
    profile = _profile_enrichment(row)
    action = str(profile.get("action") or "request_scope_decision")
    owned = profile.get("owned") is True
    ratified = (
        profile.get("profile_status") in {"reviewed", "confirmed"}
        and profile.get("journey_stage") == "validate"
    )
    junit_reason = str((row.get("enrichment") or {}).get("junit_reason") or "")
    missing_step = status == "skipped" and junit_reason == "step_not_configured"
    accepted_skip = status == "skipped" and ratified and action == SKIP_ACTION
    accepted_unowned_failure = (
        status in FAILURE_STATUSES and ratified and action == SKIP_ACTION and not owned
    )
    blocking = status != "pass"
    if accepted_skip or accepted_unowned_failure:
        blocking = False

    remediation = row.get("remediation")
    remediation = remediation if isinstance(remediation, Mapping) else {}
    target = remediation.get("target")
    edit_eligible = bool(
        blocking
        and (status in FAILURE_STATUSES or missing_step)
        and owned
        and ratified
        and action == FIX_ACTION
        and remediation.get("auto_fixable") is True
        and isinstance(target, str)
        and target
    )

    if status == "pass":
        reason = "Validation passed."
    elif accepted_skip:
        reason = "Skip is explicitly accepted by the reviewed responsibility route."
    elif accepted_unowned_failure:
        reason = "Failure is outside the ISV-owned scope and has an explicit skip disposition."
    elif missing_step:
        reason = "Validation could not run because the provider step is not configured."
    elif status == "skipped":
        reason = f"Skip is not approved; profile route is '{action}'."
    elif edit_eligible:
        reason = "Reviewed ownership and scanner policy permit a guarded candidate edit."
    else:
        reason = f"Observed status '{status}' requires route '{action}'."
    return GapDecision(blocking=blocking, edit_eligible=edit_eligible, action=action, reason=reason)


def blocking_rows(report: Mapping[str, Any], domain: str | None = None) -> list[dict[str, Any]]:
    rows = report.get("rows")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and (domain is None or row.get("domain") == domain)
        and decide_gap(row).blocking
    ]


def validation_profile_issues(
    profile: SolutionProfile | None,
    domains: Sequence[str],
) -> list[str]:
    """Return the small set of profile conditions required to enter validation."""

    if profile is None:
        return ["a reviewed solution profile is required"]

    issues: list[str] = []
    if not profile_is_ratified(profile):
        if profile.solution.profile_status not in {"reviewed", "confirmed"}:
            issues.append("profile_status must be reviewed or confirmed")
        if profile.journey.stage != "validate":
            issues.append("journey stage must be validate")

    by_domain = {item.domain: item for item in profile.domains}
    for raw_domain in domains:
        domain = canonicalize_domain(raw_domain)
        scope = by_domain.get(domain)
        if scope is None:
            issues.append(f"domain '{domain}' is missing from the profile")
            continue
        if not scope.owned:
            issues.append(f"domain '{domain}' is not ISV-owned")
            continue
        if blocks_readiness(scope.coverage, scope.validation_mode):
            issues.append(
                f"domain '{domain}' has unresolved coverage '{scope.coverage}/{scope.validation_mode}'"
            )
        for capability in scope.capabilities:
            coverage = capability.coverage or scope.coverage
            mode = capability.validation_mode or scope.validation_mode
            if blocks_readiness(coverage, mode):
                issues.append(
                    f"capability '{capability.id}' has unresolved coverage '{coverage}/{mode}'"
                )
    return issues


def _profile_enrichment(row: Mapping[str, Any]) -> Mapping[str, Any]:
    enrichment = row.get("enrichment")
    if not isinstance(enrichment, Mapping):
        return {}
    profile = enrichment.get("solution_profile")
    return profile if isinstance(profile, Mapping) else {}
