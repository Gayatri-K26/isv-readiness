from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.cli import main
from isv_readiness.scan.dynamic import DynamicArtifacts, scan_dynamic_artifacts
from isv_readiness.scan.scanner import ScanOptions, scan_provider


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
PROVIDER = FIXTURES / "provider_repo"
DYNAMIC = FIXTURES / "vm-dynamic"


class CrossDomainDynamicScanTests(unittest.TestCase):
    def test_vm_junit_preserves_reason_and_static_context(self) -> None:
        static_report = scan_provider(
            ScanOptions(
                provider_repo=PROVIDER,
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        rows = scan_dynamic_artifacts(
            DynamicArtifacts(
                provider_repo=PROVIDER,
                domain="vm",
                junit_path=DYNAMIC / "junit.xml",
                log_path=DYNAMIC / "isvctl.log",
                config_path=PROVIDER / "config" / "vm.yaml",
                static_rows=tuple(static_report.rows),
            )
        )
        by_validation = {row.validation_class: row for row in rows}

        state = by_validation["InstanceStateCheck"]
        self.assertEqual(state.status, "pass")
        self.assertEqual(state.step_name, "describe_instance")
        self.assertEqual(state.enrichment["validation_category"], "instance_info")

        listing = by_validation["InstanceListCheck"]
        self.assertEqual(listing.status, "fail")
        self.assertEqual(listing.gap_type, "provider_script")
        self.assertIn("InstanceListCheck", listing.evidence.stderr_excerpt or "")

        missing_step = by_validation["InstanceCreatedCheck"]
        self.assertEqual(missing_step.status, "skipped")
        self.assertEqual(missing_step.step_name, "launch_instance")
        self.assertEqual(missing_step.gap_type, "onboarding")
        self.assertEqual(missing_step.enrichment["junit_reason"], "step_not_configured")
        self.assertTrue(missing_step.remediation.auto_fixable)

        render_error = by_validation["StepSuccessCheck"]
        self.assertEqual(render_error.status, "error")
        self.assertEqual(render_error.enrichment["junit_reason"], "template_render_failed")
        self.assertTrue(render_error.remediation.auto_fixable)

    def test_malformed_junit_becomes_explicit_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            junit_path = Path(tempdir) / "broken.xml"
            junit_path.write_text("<testsuite>", encoding="utf-8")
            rows = scan_dynamic_artifacts(
                DynamicArtifacts(
                    provider_repo=PROVIDER,
                    domain="vm",
                    junit_path=junit_path,
                )
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "error")
        self.assertEqual(rows[0].validation_class, "JUnitContract")
        self.assertEqual(rows[0].gap_type, "lab_env")

    def test_cli_merges_static_and_dynamic_vm_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "vm-gaps.json"
            exit_code = main(
                [
                    "scan",
                    "-p",
                    str(PROVIDER),
                    "--domains",
                    "vm",
                    "--validation-root",
                    str(FIXTURES / "ai-cloud-validation"),
                    "--junit",
                    str(DYNAMIC / "junit.xml"),
                    "--log",
                    str(DYNAMIC / "isvctl.log"),
                    "--out",
                    str(output),
                ]
            )
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        schema = json.loads((ROOT / "schemas" / "gaps.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        dynamic_rows = [row for row in data["rows"] if row["detection"] == "dynamic"]
        self.assertEqual(len(dynamic_rows), 4)
        self.assertEqual({row["domain"] for row in dynamic_rows}, {"vm"})


if __name__ == "__main__":
    unittest.main()
