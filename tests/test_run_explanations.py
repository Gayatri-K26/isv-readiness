from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.run_explanations import write_run_explanations
from isv_readiness.runs import JUNIT_FILENAME, LOG_FILENAME
from isv_readiness.schema import load_schema
from tests.test_live import COMMIT, _project


class RunExplanationTests(unittest.TestCase):
    def test_completed_run_records_pass_fail_runtime_skip_and_reviewed_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=True)
            run_dir = manifest.parent / ".gapctl" / "runs" / "run-vm"
            run_dir.mkdir(parents=True)
            (run_dir / JUNIT_FILENAME).write_text("<testsuite />", encoding="utf-8")
            (run_dir / LOG_FILENAME).write_text("redacted log\n", encoding="utf-8")
            report = {
                "schema_version": "0.2.0",
                "provider_repo": str(project.provider_root(manifest)),
                "domains": ["vm"],
                "rows": [
                    _row("pass", "CreateCheck", testcase="test_create[CreateCheck]"),
                    _row(
                        "fail",
                        "DeleteCheck",
                        testcase="test_delete[DeleteCheck]",
                        validation_message="runtime_exception: Bearer abcdefghijklmnop failed",
                        junit_reason="runtime_exception",
                    ),
                    _row(
                        "skipped",
                        "CapacityCheck",
                        testcase="test_capacity[CapacityCheck]",
                        validation_message="runtime_skip: two GPU nodes are required",
                        junit_reason="runtime_skip",
                    ),
                    _row(
                        "pass",
                        "OptionalGpuCheck",
                        detection="static",
                        action="skip_with_rationale",
                        coverage="out_of_scope",
                        validation_mode="skip",
                        rationale="The reviewed product scope excludes this optional GPU check.",
                    ),
                ],
            }

            output = write_run_explanations(
                project,
                manifest,
                run_dir=run_dir,
                run_id="run-vm",
                domain="vm",
                exit_code=1,
                success=False,
                report=report,
            )

            raw = json.loads(output.read_text(encoding="utf-8"))
            jsonschema.validate(raw, load_schema("run-explanations.schema.json"))
            self.assertEqual(raw["run"]["validation_commit"], COMMIT)
            self.assertFalse(raw["run"]["success"])
            self.assertEqual(raw["artifacts"]["junit"]["path"], JUNIT_FILENAME)
            by_class = {item["validation_class"]: item for item in raw["checks"]}
            self.assertEqual(by_class["CreateCheck"]["reason_code"], "validation_passed")
            self.assertEqual(by_class["DeleteCheck"]["reason_code"], "runtime_exception")
            self.assertNotIn("abcdefghijklmnop", by_class["DeleteCheck"]["explanation"])
            self.assertEqual(by_class["CapacityCheck"]["outcome"], "skipped")
            self.assertTrue(by_class["CapacityCheck"]["decision"]["blocking"])
            scope_skip = by_class["OptionalGpuCheck"]
            self.assertEqual(scope_skip["source"], "reviewed_scope")
            self.assertEqual(scope_skip["reason_code"], "approved_scope_exclusion")
            self.assertEqual(scope_skip["scope"]["evidence_refs"], ["scope-survey"])


def _row(
    status: str,
    validation_class: str,
    *,
    detection: str = "dynamic",
    testcase: str | None = None,
    validation_message: str | None = None,
    junit_reason: str | None = None,
    action: str = "implement_or_fix_adapter",
    coverage: str = "covered",
    validation_mode: str = "test",
    rationale: str = "The reviewed provider owns this validation.",
) -> dict:
    return {
        "id": f"gap-{validation_class}",
        "domain": "vm",
        "step_name": validation_class.removesuffix("Check").lower(),
        "validation_class": validation_class,
        "requirement_id": f"requirement-{validation_class}",
        "status": status,
        "detection": detection,
        "stage": "coverage" if status == "skipped" else "correctness",
        "evidence": {
            "message": f"{validation_class} {status} result.",
            "validation_message": validation_message,
            "schema_errors": [],
            "missing_json_fields": [],
            "stderr_excerpt": None,
            "script_path": None,
            "config_path": "config/vm.yaml",
        },
        "remediation": {
            "auto_fixable": status in {"fail", "not_implemented", "error"},
            "target": "scripts/vm/check.py" if status in {"fail", "not_implemented", "error"} else None,
            "rerun_command": "isvctl test run -f config/vm.yaml",
            "aws_reference": None,
        },
        "enrichment": {
            "junit_testcase": testcase,
            "junit_reason": junit_reason,
            "solution_profile": {
                "profile_id": "acme-profile",
                "profile_status": "reviewed",
                "journey_stage": "validate",
                "matched": True,
                "owned": True,
                "capability_id": f"capability-{validation_class}",
                "coverage": coverage,
                "validation_mode": validation_mode,
                "capability_owner_actor_id": "acme",
                "provider_adapter_owner_actor_id": "acme",
                "component_ids": ["acme-platform"],
                "action": action,
                "rationale": rationale,
                "required_inputs": [],
                "evidence_refs": ["scope-survey"],
            },
        },
        "labels": [],
    }


if __name__ == "__main__":
    unittest.main()
