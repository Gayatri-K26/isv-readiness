from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.loop import LoopStateError, advance_loop, load_loop_state
from isv_readiness.schema import load_schema

ROOT = Path(__file__).resolve().parents[1]


def _row(
    gap_id: str,
    *,
    status: str = "not_implemented",
    action: str = "implement_or_fix_adapter",
    auto_fixable: bool = True,
    labels: list[str] | None = None,
) -> dict:
    return {
        "id": gap_id,
        "domain": "vm",
        "step_name": gap_id,
        "validation_class": "InstanceCreatedCheck",
        "requirement_id": None,
        "labels": labels or [],
        "status": status,
        "detection": "static",
        "stage": "coverage",
        "evidence": {
            "message": "test",
            "validation_message": None,
            "schema_errors": [],
            "missing_json_fields": [],
            "stderr_excerpt": None,
            "script_path": "scripts/vm/test.py",
            "config_path": "config/vm.yaml",
        },
        "remediation": {
            "auto_fixable": auto_fixable,
            "target": "scripts/vm/test.py" if auto_fixable else None,
            "rerun_command": "isvctl test run -f config/vm.yaml",
            "aws_reference": None,
        },
        "enrichment": {
            "solution_profile": {
                "action": action,
                "owned": True,
                "profile_status": "reviewed",
                "journey_stage": "validate",
            }
        },
    }


def _report(rows: list[dict]) -> dict:
    return {
        "schema_version": "0.2.0",
        "provider_repo": "/provider",
        "domains": ["vm"],
        "rows": rows,
    }


class LoopControllerTests(unittest.TestCase):
    def test_fixable_adapter_work_advances_before_noneditable_adapter_gap(self) -> None:
        state = advance_loop(
            _report(
                [
                    _row("gap_aaa_nonedit", auto_fixable=False),
                    _row("gap_zzz_fixable"),
                ]
            ),
            domain="vm",
        )

        self.assertEqual(state.status, "ready")
        self.assertEqual(state.selected_gap_id, "gap_zzz_fixable")

    def test_scope_blocker_is_selected_before_fixable_work(self) -> None:
        state = advance_loop(
            _report(
                [
                    _row("gap_fixable0001"),
                    _row("gap_scope000001", action="request_scope_decision", auto_fixable=False),
                ]
            ),
            domain="vm",
        )

        self.assertEqual(state.status, "blocked")
        self.assertEqual(state.selected_gap_id, "gap_scope000001")
        self.assertEqual(state.route, "request_scope_decision")
        self.assertEqual(state.unresolved_count, 2)

    def test_minimum_requirement_breaks_ties_between_fixable_rows(self) -> None:
        state = advance_loop(
            _report(
                [
                    _row("gap_aaa_optional"),
                    _row("gap_zzz_minimum", labels=["vm", "min_req"]),
                ]
            ),
            domain="vm",
        )

        self.assertEqual(state.status, "ready")
        self.assertEqual(state.selected_gap_id, "gap_zzz_minimum")

    def test_retry_budget_requires_explicit_attempt_records(self) -> None:
        report = _report([_row("gap_retry000001")])
        initial = advance_loop(report, domain="vm", max_attempts=2)
        self.assertEqual(initial.status, "ready")
        self.assertEqual(initial.attempts_by_gap, {})

        attempted_once = advance_loop(
            report,
            domain="vm",
            previous=initial,
            attempted_gap_id="gap_retry000001",
            max_attempts=2,
        )
        self.assertEqual(attempted_once.status, "ready")
        self.assertEqual(attempted_once.attempts_by_gap["gap_retry000001"], 1)

        exhausted = advance_loop(
            report,
            domain="vm",
            previous=attempted_once,
            attempted_gap_id="gap_retry000001",
            max_attempts=2,
        )
        self.assertEqual(exhausted.status, "blocked")
        self.assertIn("Retry budget exhausted", exhausted.reason)
        self.assertEqual(exhausted.attempts_by_gap["gap_retry000001"], 2)

    def test_passes_and_approved_skips_complete_the_loop(self) -> None:
        state = advance_loop(
            _report(
                [
                    _row("gap_pass0000001", status="pass"),
                    _row(
                        "gap_skip0000001",
                        status="skipped",
                        action="skip_with_rationale",
                        auto_fixable=False,
                    ),
                ]
            ),
            domain="vm",
        )

        self.assertEqual(state.status, "complete")
        self.assertEqual(state.unresolved_count, 0)
        self.assertIsNone(state.selected_gap_id)

    def test_rejects_attempt_for_any_gap_other_than_previous_selection(self) -> None:
        report = _report([_row("gap_selected001")])
        initial = advance_loop(report, domain="vm")
        with self.assertRaisesRegex(LoopStateError, "does not match"):
            advance_loop(
                report,
                domain="vm",
                previous=initial,
                attempted_gap_id="gap_other00001",
            )

    def test_persists_and_loads_loop_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / "loop-state.json"
            state = advance_loop(_report([_row("gap_state000001")]), domain="vm")
            state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
            persisted = load_loop_state(state_path)

            schema = load_schema("loop-state.schema.json")
            jsonschema.Draft202012Validator(schema).validate(persisted.to_dict())

        self.assertEqual(persisted.status, "ready")
        self.assertEqual(persisted.selected_gap_id, "gap_state000001")
        self.assertEqual(len(persisted.history), 1)


if __name__ == "__main__":
    unittest.main()
