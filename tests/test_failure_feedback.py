from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from isv_readiness.agent import _live_failure_envelopes
from isv_readiness.failure_feedback import (
    artifact_reference,
    redact_failure_text,
    stable_failure_fingerprint,
)


class FailureFeedbackTests(unittest.TestCase):
    def test_fingerprint_ignores_common_volatile_run_values(self) -> None:
        first = stable_failure_fingerprint(
            "live",
            "Operation failed at 2026-07-29T12:01:02Z",
            (
                "request_id=45c0b895-5d25-42a0-a526-bc2bdc5ba821 poll 4",
            ),
        )
        second = stable_failure_fingerprint(
            "live",
            "Operation failed at 2026-07-29T12:09:55Z",
            (
                "request_id=de305d54-75b4-431b-adb2-eb6b9e546014 poll 19",
            ),
        )

        self.assertEqual(first, second)

    def test_failure_text_redacts_secrets_and_email_like_pii(self) -> None:
        redacted = redact_failure_text(
            "TOKEN=plain-secret\ncontact owner@example.com\nBearer abcdefghijklmnop"
        )

        self.assertNotIn("plain-secret", redacted)
        self.assertNotIn("owner@example.com", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)

    def test_artifact_reference_keeps_path_and_content_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            artifact = Path(tempdir) / "run.log"
            artifact.write_text("diagnostic\n", encoding="utf-8")

            reference = artifact_reference("log", artifact)

        assert reference is not None
        self.assertEqual(reference["path"], str(artifact))
        self.assertEqual(len(reference["sha256"]), 64)

    def test_live_feedback_retries_only_evidenced_editable_failures(self) -> None:
        editable = _row(owned=True, action="implement_or_fix_adapter", auto_fixable=True)

        feedback = _live_failure_envelopes(
            (editable,),
            attempt=1,
            exit_code=1,
            selected_statuses=("error",),
            artifact_refs=(),
            log_path=Path("/not/present"),
        )

        self.assertTrue(feedback[0]["retryable"])
        self.assertEqual(feedback[0]["affected_checks"], ["ExampleCheck"])
        self.assertIn("provider returned 409", feedback[0]["stable_error"])

    def test_unowned_failure_conflict_is_parked_for_scope_triage(self) -> None:
        disputed = _row(owned=False, action="skip_with_rationale", auto_fixable=False)

        feedback = _live_failure_envelopes(
            (disputed,),
            attempt=1,
            exit_code=1,
            selected_statuses=("error",),
            artifact_refs=(),
            log_path=Path("/not/present"),
        )

        self.assertFalse(feedback[0]["retryable"])
        self.assertIn("scope decision", feedback[0]["retry_reason"])


def _row(*, owned: bool, action: str, auto_fixable: bool) -> dict:
    return {
        "id": "gap_example000001",
        "domain": "vm",
        "status": "error",
        "detection": "dynamic",
        "validation_class": "ExampleCheck",
        "evidence": {
            "message": "ExampleCheck errored: provider returned 409",
            "validation_message": "provider returned 409",
        },
        "remediation": {
            "auto_fixable": auto_fixable,
            "target": "config/vm.yaml" if auto_fixable else None,
        },
        "enrichment": {
            "solution_profile": {
                "owned": owned,
                "action": action,
                "profile_status": "reviewed",
                "journey_stage": "validate",
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
