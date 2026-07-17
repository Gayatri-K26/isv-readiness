from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from isv_readiness.decision import FAILURE_STATUSES, FIX_ACTION, decide_gap

LOOP_STATE_VERSION = "0.1.0"
UNRESOLVED_STATUSES = set(FAILURE_STATUSES)  # compatibility for external callers
ACTION_PRIORITY = {
    "request_scope_decision": 0,
    "collect_evidence": 1,
    "request_external_adapter": 2,
    "record_product_gap": 3,
    FIX_ACTION: 4,
}

LoopStatus = Literal["ready", "blocked", "complete"]


class LoopStateError(ValueError):
    """Raised when loop state cannot advance safely."""


@dataclass(frozen=True)
class LoopHistoryEntry:
    report_fingerprint: str
    status: LoopStatus
    selected_gap_id: str | None
    route: str | None
    reason: str


@dataclass(frozen=True)
class LoopState:
    schema_version: str
    domain: str
    status: LoopStatus
    selected_gap_id: str | None
    route: str | None
    reason: str
    report_fingerprint: str
    unresolved_count: int
    attempts_by_gap: dict[str, int] = field(default_factory=dict)
    history: tuple[LoopHistoryEntry, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["history"] = [asdict(entry) for entry in self.history]
        return payload


def load_loop_state(path: Path) -> LoopState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != LOOP_STATE_VERSION:
        raise LoopStateError(f"Unsupported loop state version: {payload.get('schema_version', '<missing>')}")
    history = tuple(LoopHistoryEntry(**item) for item in payload.get("history", []))
    attempts = payload.get("attempts_by_gap") or {}
    if not isinstance(attempts, dict) or not all(
        isinstance(key, str) and isinstance(value, int) and value >= 0 for key, value in attempts.items()
    ):
        raise LoopStateError("Loop attempts_by_gap must map gap IDs to non-negative integers.")
    return LoopState(
        schema_version=payload["schema_version"],
        domain=payload["domain"],
        status=payload["status"],
        selected_gap_id=payload.get("selected_gap_id"),
        route=payload.get("route"),
        reason=payload["reason"],
        report_fingerprint=payload["report_fingerprint"],
        unresolved_count=payload["unresolved_count"],
        attempts_by_gap={str(key): int(value) for key, value in attempts.items()},
        history=history,
    )


def advance_loop(
    report: dict[str, Any],
    *,
    domain: str,
    previous: LoopState | None = None,
    attempted_gap_id: str | None = None,
    max_attempts: int = 3,
) -> LoopState:
    if max_attempts < 1:
        raise LoopStateError("max_attempts must be at least 1.")
    if previous is not None and previous.domain != domain:
        raise LoopStateError(f"Loop state domain '{previous.domain}' does not match requested domain '{domain}'.")

    attempts = dict(previous.attempts_by_gap) if previous else {}
    if attempted_gap_id is not None:
        if previous is None or previous.selected_gap_id != attempted_gap_id:
            expected = previous.selected_gap_id if previous else None
            raise LoopStateError(
                f"Attempted gap '{attempted_gap_id}' does not match previously selected gap '{expected}'."
            )
        attempts[attempted_gap_id] = attempts.get(attempted_gap_id, 0) + 1

    report_fingerprint = _report_fingerprint(report, domain)
    unresolved = [
        row
        for row in report.get("rows", [])
        if row.get("domain") == domain
        and decide_gap(row).blocking
    ]
    unresolved.sort(key=_selection_key)

    if not unresolved:
        return _state(
            domain=domain,
            status="complete",
            selected_gap_id=None,
            route=None,
            reason="No blocking validation outcomes remain for the selected domain.",
            report_fingerprint=report_fingerprint,
            unresolved_count=0,
            attempts=attempts,
            previous=previous,
        )

    selected = unresolved[0]
    gap_id = str(selected.get("id"))
    gap_decision = decide_gap(selected)
    action = gap_decision.action
    if not gap_decision.edit_eligible:
        return _state(
            domain=domain,
            status="blocked",
            selected_gap_id=gap_id,
            route=action,
            reason=gap_decision.reason,
            report_fingerprint=report_fingerprint,
            unresolved_count=len(unresolved),
            attempts=attempts,
            previous=previous,
        )

    attempt_count = attempts.get(gap_id, 0)
    if attempt_count >= max_attempts:
        return _state(
            domain=domain,
            status="blocked",
            selected_gap_id=gap_id,
            route=action,
            reason=f"Retry budget exhausted for {gap_id}: {attempt_count}/{max_attempts} attempts.",
            report_fingerprint=report_fingerprint,
            unresolved_count=len(unresolved),
            attempts=attempts,
            previous=previous,
        )

    return _state(
        domain=domain,
        status="ready",
        selected_gap_id=gap_id,
        route=action,
        reason=(f"Gap is eligible for a guarded patch proposal; {attempt_count}/{max_attempts} attempts recorded."),
        report_fingerprint=report_fingerprint,
        unresolved_count=len(unresolved),
        attempts=attempts,
        previous=previous,
    )


def _state(
    *,
    domain: str,
    status: LoopStatus,
    selected_gap_id: str | None,
    route: str | None,
    reason: str,
    report_fingerprint: str,
    unresolved_count: int,
    attempts: dict[str, int],
    previous: LoopState | None,
) -> LoopState:
    entry = LoopHistoryEntry(
        report_fingerprint=report_fingerprint,
        status=status,
        selected_gap_id=selected_gap_id,
        route=route,
        reason=reason,
    )
    history = (*previous.history, entry) if previous else (entry,)
    return LoopState(
        schema_version=LOOP_STATE_VERSION,
        domain=domain,
        status=status,
        selected_gap_id=selected_gap_id,
        route=route,
        reason=reason,
        report_fingerprint=report_fingerprint,
        unresolved_count=unresolved_count,
        attempts_by_gap=attempts,
        history=history[-100:],
    )


def _selection_key(row: dict[str, Any]) -> tuple[int, int, int, int, str, str, str]:
    decision = decide_gap(row)
    fixability_priority = 0 if decision.edit_eligible else 1
    minimum_priority = 0 if "min_req" in row.get("labels", []) else 1
    stage_priority = 0 if row.get("stage") == "coverage" else 1
    return (
        ACTION_PRIORITY.get(decision.action, 0),
        fixability_priority,
        minimum_priority,
        stage_priority,
        str(row.get("step_name", "")),
        str(row.get("validation_class", "")),
        str(row.get("id", "")),
    )


def _report_fingerprint(report: dict[str, Any], domain: str) -> str:
    payload = {
        "schema_version": report.get("schema_version"),
        "provider_repo": report.get("provider_repo"),
        "domain": domain,
        "rows": [row for row in report.get("rows", []) if row.get("domain") == domain],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
