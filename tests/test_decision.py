from __future__ import annotations

import unittest

from isv_readiness.decision import adapter_contract_unit, decide_gap


def _row(
    *,
    status: str,
    action: str = "implement_or_fix_adapter",
    owned: bool = True,
    profile_status: str = "reviewed",
    journey_stage: str = "validate",
    junit_reason: str | None = None,
    step_name: str = "launch",
    target: str = "scripts/vm/launch.py",
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
        "id": "gap_0123456789ab",
        "step_name": step_name,
        "status": status,
        "remediation": {
            "auto_fixable": True,
            "target": target,
        },
        "enrichment": enrichment,
    }


class GapDecisionTests(unittest.TestCase):
    def test_adapter_contract_units_split_shared_config_by_step(self) -> None:
        launch_config = _row(
            status="not_implemented",
            step_name="launch",
            target="config/vm.yaml",
        )
        describe_config = _row(
            status="not_implemented",
            step_name="describe",
            target="config/vm.yaml",
        )
        launch_config_sibling = {**launch_config, "id": "gap_abcdef012345"}

        self.assertEqual(
            adapter_contract_unit(launch_config),
            adapter_contract_unit(launch_config_sibling),
        )
        self.assertNotEqual(
            adapter_contract_unit(launch_config),
            adapter_contract_unit(describe_config),
        )

        launch_script = _row(
            status="not_implemented",
            step_name="launch",
            target="scripts/vm/shared.py",
        )
        describe_script = _row(
            status="not_implemented",
            step_name="describe",
            target="scripts/vm/shared.py",
        )
        self.assertEqual(
            adapter_contract_unit(launch_script),
            adapter_contract_unit(describe_script),
        )

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
