from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.scan.models import SCHEMA_VERSION
from isv_readiness.scan.report import render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema

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
        schema = load_schema("gaps.schema.json")
        jsonschema.Draft202012Validator(schema).validate(data)

        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        by_step = {(row.step_name, row.validation_class): row for row in report.rows}

        launch = by_step[("launch_instance", "InstanceCreatedCheck")]
        self.assertEqual(launch.status, "not_implemented")
        self.assertEqual(launch.stage, "coverage")
        self.assertTrue(launch.remediation.auto_fixable)
        self.assertEqual(launch.remediation.target, "scripts/vm/launch_instance.py")
        self.assertEqual(launch.requirement_id, "VM01-01")
        self.assertEqual(launch.labels, ("vm", "min_req"))

        listing = by_step[("list_instances", "InstanceListCheck")]
        self.assertEqual(listing.status, "fail")
        self.assertEqual(listing.stage, "correctness")
        self.assertIn("instances", listing.evidence.missing_json_fields)

        missing = by_step[("describe_instance", "InstanceStateCheck")]
        self.assertEqual(missing.status, "not_implemented")
        # The unwired step's fix is wiring in the ISV-owned domain config —
        # inside the guarded surface, so the agent may draft it.
        self.assertTrue(missing.remediation.auto_fixable)
        self.assertEqual(missing.remediation.target, "config/vm.yaml")
        self.assertIsNotNone(missing.remediation.aws_reference)

        teardown = by_step[("teardown", "StepSuccessCheck")]
        self.assertEqual(teardown.status, "skipped")
        self.assertFalse(teardown.remediation.auto_fixable)

    def test_static_scan_preserves_declared_execution_order(self) -> None:
        report = scan_provider(
            ScanOptions(
                provider_repo=FIXTURES / "provider_repo",
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )

        self.assertEqual(
            [row.step_name for row in report.rows],
            ["launch_instance", "list_instances", "describe_instance", "teardown"],
        )

    def test_static_scan_orders_by_phase_then_step_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            validation_root = Path(tempdir) / "validation"
            validation_root.mkdir()
            config = provider / "config" / "vm.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "commands:\n"
                "  vm:\n"
                "    phases: [setup, test, teardown]\n"
                "    steps:\n"
                "      - name: zulu_test\n"
                "        phase: test\n"
                "        command: python ../scripts/vm/zulu_test.py\n"
                "      - name: alpha_setup\n"
                "        phase: setup\n"
                "        command: python ../scripts/vm/alpha_setup.py\n"
                "      - name: beta_test\n"
                "        phase: test\n"
                "        command: python ../scripts/vm/beta_test.py\n"
                "      - name: aardvark_teardown\n"
                "        phase: teardown\n"
                "        command: python ../scripts/vm/aardvark_teardown.py\n",
                encoding="utf-8",
            )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=validation_root,
                )
            )

        self.assertEqual(
            [row.step_name for row in report.rows],
            ["alpha_setup", "zulu_test", "beta_test", "aardvark_teardown"],
        )

    def test_scan_report_rendering(self) -> None:
        report = scan_provider(
            ScanOptions(
                provider_repo=FIXTURES / "provider_repo",
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        ).to_dict()
        scorecard = render_report(report, "scorecard")
        tree = render_report(report, "tree")
        markdown = render_report(report, "md")

        self.assertIn("Gap Scorecard", scorecard)
        self.assertIn("not_implemented=2", scorecard)
        self.assertIn("describe_instance", tree)
        self.assertIn("| ID | Requirement | Labels | Domain | Step |", markdown)

    def test_python_comments_are_not_stubs_and_syntax_errors_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            script = provider / "scripts" / "vm" / "launch_instance.py"
            script.write_text(
                "import json\n\n"
                "# TODO: improve retries\n"
                'print(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-1"}))\n',
                encoding="utf-8",
            )
            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "pass")

            script.write_text("def broken(:\n", encoding="utf-8")
            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "error")
            self.assertIn("invalid Python syntax", row.evidence.message)
            self.assertTrue(row.remediation.auto_fixable)

    def test_scan_rejects_required_downstream_outputs_left_definitely_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            script = provider / "scripts" / "vm" / "launch_instance.py"
            config = provider / "config" / "vm.yaml"
            script.parent.mkdir(parents=True)
            config.parent.mkdir()
            config.write_text(
                "import:\n"
                "  - vm.yaml\n"
                "commands:\n"
                "  vm:\n"
                "    steps:\n"
                "      - name: launch_instance\n"
                "        command: python ../scripts/vm/launch_instance.py\n"
                "      - name: list_instances\n"
                "        command: python ../scripts/vm/list_instances.py\n"
                "        args:\n"
                "          - '{{steps.launch_instance.public_ip}}'\n"
                "          - '{{steps.launch_instance.key_file}}'\n",
                encoding="utf-8",
            )
            (script.parent / "list_instances.py").write_text(
                "print({'success': True, 'platform': 'fixture', 'instances': []})\n",
                encoding="utf-8",
            )
            script.write_text(
                "unused = {\n"
                "    'success': True, 'platform': 'fixture',\n"
                "    'public_ip': '', 'key_file': '',\n"
                "}\n"
                "result = {\n"
                "    'success': True, 'platform': 'fixture', 'instance_id': 'vm-1',\n"
                "    'public_ip': '', 'key_file': '',\n"
                "}\n"
                "print(result)\n",
                encoding="utf-8",
            )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "fail")
            self.assertIn("definitely empty", row.evidence.message)
            self.assertIn("public_ip", " ".join(row.evidence.schema_errors))
            self.assertIn("key_file", " ".join(row.evidence.schema_errors))

            script.write_text(
                "result = {\n"
                "    'success': True, 'platform': 'fixture', 'instance_id': 'vm-1',\n"
                "}\n"
                "print(result)\n",
                encoding="utf-8",
            )
            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "fail")
            self.assertIn("public_ip", " ".join(row.evidence.schema_errors))
            self.assertIn("key_file", " ".join(row.evidence.schema_errors))

            script.write_text(
                "result = {\n"
                "    'success': True, 'platform': 'fixture', 'instance_id': 'vm-1',\n"
                "}\n"
                "result.update(resolve_runtime_fields())\n"
                "print(result)\n",
                encoding="utf-8",
            )
            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "pass")

            script.write_text(
                "unused = {\n"
                "    'success': True, 'platform': 'fixture',\n"
                "    'public_ip': '', 'key_file': '',\n"
                "}\n"
                "result = {\n"
                "    'success': True, 'platform': 'fixture', 'instance_id': 'vm-1',\n"
                "    'public_ip': '', 'key_file': '',\n"
                "}\n"
                "result['public_ip'] = resolve_host()\n"
                "result['key_file'] = resolve_key()\n"
                "print(result)\n",
                encoding="utf-8",
            )
            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=FIXTURES / "ai-cloud-validation",
                )
            )
            row = next(item for item in report.rows if item.step_name == "launch_instance")
            self.assertEqual(row.status, "pass")

    def test_skipped_consumer_does_not_require_producer_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            script = provider / "scripts" / "vm" / "launch_instance.py"
            config = provider / "config" / "vm.yaml"
            script.parent.mkdir(parents=True)
            config.parent.mkdir()
            config.write_text(
                "commands:\n"
                "  vm:\n"
                "    steps:\n"
                "      - name: launch_instance\n"
                "        command: python ../scripts/vm/launch_instance.py\n"
                "      - name: teardown\n"
                "        skip: true\n"
                "        command: python ../scripts/vm/teardown.py\n"
                "        args: ['{{steps.launch_instance.security_group_id}}']\n",
                encoding="utf-8",
            )
            script.write_text(
                'print({"success": True, "platform": "fixture", "instance_id": "node-1"})\n',
                encoding="utf-8",
            )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=Path(tempdir) / "validation",
                )
            )

        launch = next(row for row in report.rows if row.step_name == "launch_instance")
        self.assertEqual(launch.status, "pass")

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

        node_count = by_step[("<validation>", "K8sNodeCountCheck")]
        self.assertEqual(node_count.status, "pass")
        self.assertEqual(node_count.enrichment["validation_category"], "kubernetes")
        self.assertFalse(node_count.enrichment["requires_provider_step"])

        cpu_pool = by_step[("create_test_node_pool", "K8sNodePoolCheck")]
        gpu_pool = by_step[("create_test_gpu_node_pool", "K8sNodePoolCheck")]
        self.assertEqual(cpu_pool.status, "not_implemented")
        self.assertEqual(gpu_pool.status, "not_implemented")

        storage = by_step[("inspect_storage", "K8sCsiStorageTypesCheck")]
        self.assertEqual(storage.status, "not_implemented")
        self.assertEqual(storage.enrichment["validation_phase"], "test")

        reframe = by_step[("<validation>", "CPUInfoCheck")]
        self.assertEqual(reframe.enrichment["execution_adapter"], "reframe")
        self.assertEqual(len(report.rows), len({row.id for row in report.rows}))

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

        row = next(row for row in report.rows if "my-isv template" in row.evidence.message)
        self.assertEqual(row.status, "not_implemented")
        self.assertTrue(row.remediation.auto_fixable)
        self.assertIn("my-isv template", row.evidence.message)

    def test_malformed_validation_contract_emits_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            config_dir = provider / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "vm.yaml").write_text(
                "version: '1.0'\ntests:\n  platform: vm\n  validations: []\n",
                encoding="utf-8",
            )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=Path(tempdir) / "empty-validation-root",
                )
            )

        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.step_name, "<validation>")
        self.assertEqual(row.validation_class, "<invalid>")
        self.assertEqual(row.status, "error")
        self.assertFalse(row.remediation.auto_fixable)
        self.assertIn("tests.validations", row.evidence.message)

    def test_repeated_validation_instances_get_unique_gap_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "provider"
            config_dir = provider / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "vm.yaml").write_text(
                """version: '1.0'
tests:
  platform: vm
  validations:
    repeated:
      - RepeatedCheck:
          step: inspect
          variant: first
      - RepeatedCheck:
          step: inspect
          variant: second
""",
                encoding="utf-8",
            )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["vm"],
                    validation_root=Path(tempdir) / "empty-validation-root",
                )
            )

        self.assertEqual(len(report.rows), 2)
        self.assertEqual(len({row.id for row in report.rows}), 2)
        self.assertEqual(
            {row.enrichment["validation_instance"] for row in report.rows},
            {"repeated:1", "repeated:2"},
        )

    def test_k8s_scan_reports_missing_config_as_fixable(self) -> None:
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "missing-run-provider"
        data = scan_provider(
            ScanOptions(
                provider_repo=provider,
                domains=["kubernetes"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        ).to_dict()

        self.assertTrue(data["rows"][0]["remediation"]["auto_fixable"])
        self.assertIn("No Kubernetes provider wrapper", data["rows"][0]["evidence"]["message"])


if __name__ == "__main__":
    unittest.main()
