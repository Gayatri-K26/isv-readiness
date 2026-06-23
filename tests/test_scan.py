from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.scan.models import SCHEMA_VERSION
from isv_readiness.scan.report import render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class StaticScanTests(unittest.TestCase):
    def test_static_scan_emits_schema_valid_gap_rows(self) -> None:
        report = scan_provider(
            ScanOptions(
                provider_repo=FIXTURES / "provider_repo",
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        data = report.to_dict()
        schema = json.loads((ROOT / "schemas" / "gaps.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)

        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        by_step = {(row.step_name, row.validation_class): row for row in report.rows}

        launch = by_step[("launch_instance", "InstanceCreatedCheck")]
        self.assertEqual(launch.status, "not_implemented")
        self.assertEqual(launch.stage, "coverage")
        self.assertTrue(launch.remediation.auto_fixable)
        self.assertEqual(launch.remediation.target, "scripts/vm/launch_instance.py")

        listing = by_step[("list_instances", "InstanceListCheck")]
        self.assertEqual(listing.status, "fail")
        self.assertEqual(listing.stage, "correctness")
        self.assertIn("instances", listing.evidence.missing_json_fields)

        missing = by_step[("describe_instance", "InstanceStateCheck")]
        self.assertEqual(missing.status, "not_implemented")
        self.assertFalse(missing.remediation.auto_fixable)
        self.assertIsNotNone(missing.remediation.aws_reference)

        teardown = by_step[("teardown", "StepSuccessCheck")]
        self.assertEqual(teardown.status, "skipped")
        self.assertEqual(teardown.gap_type, "semantic_mismatch")

    def test_cli_scan_and_report_rendering(self) -> None:
        from isv_readiness.cli import main

        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "gaps.json"
            exit_code = main(
                [
                    "scan",
                    "-p",
                    str(FIXTURES / "provider_repo"),
                    "--domains",
                    "vm",
                    "--validation-root",
                    str(FIXTURES / "ai-cloud-validation"),
                    "--out",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())

            report = json.loads(output.read_text(encoding="utf-8"))
            scorecard = render_report(report, "scorecard")
            tree = render_report(report, "tree")
            markdown = render_report(report, "md")

            self.assertIn("Gap Scorecard", scorecard)
            self.assertIn("not_implemented=2", scorecard)
            self.assertIn("describe_instance", tree)
            self.assertIn("| ID | Domain | Step |", markdown)

    def test_k8s_scan_finds_top_level_provider_wrapper(self) -> None:
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "dsx-air"
        report = scan_provider(
            ScanOptions(
                provider_repo=provider,
                domains=["k8s"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        by_step = {(row.step_name, row.validation_class): row for row in report.rows}

        setup = by_step[("setup", "StepOutputSchema")]
        self.assertEqual(setup.status, "pass")
        self.assertEqual(setup.evidence.config_path, str(provider.with_suffix(".yaml")))
        self.assertEqual(setup.evidence.script_path, "scripts/k8s/setup.sh")

        teardown = by_step[("teardown", "StepOutputSchema")]
        self.assertEqual(teardown.status, "pass")

    def test_k8s_scan_reports_missing_provider_wrapper_as_onboarding_gap(self) -> None:
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "new-k8s"
        report = scan_provider(
            ScanOptions(
                provider_repo=provider,
                domains=["k8s"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )

        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.status, "not_implemented")
        self.assertEqual(row.gap_type, "onboarding")
        self.assertTrue(row.remediation.auto_fixable)
        self.assertIn("No Kubernetes provider wrapper", row.evidence.message)
        self.assertTrue((row.remediation.target or "").endswith("new-k8s.yaml"))

    def test_k8s_scan_flags_my_isv_template_command_wiring(self) -> None:
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "bad-k8s"
        report = scan_provider(
            ScanOptions(
                provider_repo=provider,
                domains=["k8s"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )

        row = report.rows[0]
        self.assertEqual(row.status, "not_implemented")
        self.assertEqual(row.gap_type, "onboarding")
        self.assertTrue(row.remediation.auto_fixable)
        self.assertIn("my-isv template", row.evidence.message)


if __name__ == "__main__":
    unittest.main()
