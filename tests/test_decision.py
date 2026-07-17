from __future__ import annotations

import unittest

from isv_readiness.decision import decide_gap


def _row(
    *,
    status: str,
    action: str = "implement_or_fix_adapter",
    owned: bool = True,
    profile_status: str = "reviewed",
    journey_stage: str = "validate",
    junit_reason: str | None = None,
) -> dict:
    enrichment = {
        "solution_profile": {
            "action": action,
            "owned": owned,
            "profile_status": profile_status,
            "journey_stage": journey_stage,
        }
    }
    if junit_reason:
        enrichment["junit_reason"] = junit_reason
    return {
        "status": status,
        "remediation": {
            "auto_fixable": True,
            "target": "scripts/vm/launch.py",
        },
        "enrichment": enrichment,
    }


class GapDecisionTests(unittest.TestCase):
    def test_reviewed_owned_failure_can_generate_but_draft_cannot(self) -> None:
        reviewed = decide_gap(_row(status="not_implemented"))
        self.assertTrue(reviewed.blocking)
        self.assertTrue(reviewed.edit_eligible)

        draft = decide_gap(
            _row(status="not_implemented", profile_status="draft", journey_stage="qualify")
        )
        self.assertTrue(draft.blocking)
        self.assertFalse(draft.edit_eligible)

    def test_skip_requires_an_explicit_profile_route(self) -> None:
        unapproved = decide_gap(_row(status="skipped"))
        self.assertTrue(unapproved.blocking)
        self.assertFalse(unapproved.edit_eligible)

        approved = decide_gap(_row(status="skipped", action="skip_with_rationale"))
        self.assertFalse(approved.blocking)
        self.assertFalse(approved.edit_eligible)

        draft_skip = decide_gap(
            _row(
                status="skipped",
                action="skip_with_rationale",
                profile_status="draft",
                journey_stage="qualify",
            )
        )
        self.assertTrue(draft_skip.blocking)

    def test_missing_step_keeps_raw_skip_but_can_generate_a_candidate(self) -> None:
        decision = decide_gap(_row(status="skipped", junit_reason="step_not_configured"))
        self.assertTrue(decision.blocking)
        self.assertTrue(decision.edit_eligible)

    def test_unowned_failure_is_resolved_only_by_explicit_skip_route(self) -> None:
        accepted = decide_gap(
            _row(status="fail", action="skip_with_rationale", owned=False)
        )
        self.assertFalse(accepted.blocking)

        undecided = decide_gap(_row(status="fail", action="request_scope_decision", owned=False))
        self.assertTrue(undecided.blocking)


if __name__ == "__main__":
    unittest.main()
