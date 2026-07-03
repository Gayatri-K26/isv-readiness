from __future__ import annotations

from typing import Any

UNRESOLVED_STATUSES = {"fail", "not_implemented", "error"}


class VerificationError(ValueError):
    """Raised when verification or controlled application cannot proceed safely."""


def find_regressions(
    baseline: dict[str, Any],
    rescanned: dict[str, Any],
    *,
    domain: str,
    selected_gap_id: str,
) -> list[str]:
    """Shared isolated-rescan diff: flag prior passes that broke or new failures.

    Used by the multi-file change verifier to reject a candidate that resolves the
    selected gap but regresses any other static row in the domain.
    """
    before = {
        str(row.get("id")): row
        for row in baseline.get("rows", [])
        if row.get("domain") == domain and row.get("detection") == "static"
    }
    after = {str(row.get("id")): row for row in rescanned.get("rows", []) if row.get("domain") == domain}
    regressions: list[str] = []
    for gap_id, row in before.items():
        if gap_id == selected_gap_id:
            continue
        updated = after.get(gap_id)
        if row.get("status") == "pass" and (updated is None or updated.get("status") != "pass"):
            regressions.append(f"{gap_id}: prior pass no longer passes")
    for gap_id, row in after.items():
        if gap_id != selected_gap_id and gap_id not in before and row.get("status") in UNRESOLVED_STATUSES:
            regressions.append(f"{gap_id}: new unresolved {row.get('status')} row")
    return sorted(regressions)
