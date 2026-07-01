from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import jsonschema

from isv_readiness.validation_adapter import (
    ADAPTER_CONTRACT_VERSION,
    IsvctlAdapter,
    ValidationAdapterError,
    normalize_catalog,
    normalize_validation_plan,
)

CATALOG_PAYLOAD = {
    "isvTestVersion": "0.8.0",
    "entries": [
        {
            "name": "K8sNodeCountCheck",
            "description": "Checks the Kubernetes node count.",
            "labels": ["kubernetes"],
            "module": "isvtest.k8s",
            "platforms": ["KUBERNETES"],
        },
        {
            "name": "K8sNodePoolCheck",
            "description": "Checks a Kubernetes node pool.",
            "labels": ["kubernetes", "lifecycle"],
            "module": "isvtest.k8s",
            "platforms": ["KUBERNETES"],
        },
        {
            "name": "K8sCsiStorageTypesCheck",
            "description": "Checks advertised CSI storage types.",
            "labels": ["kubernetes", "storage"],
            "module": "isvtest.k8s",
            "platforms": ["KUBERNETES"],
        },
    ],
}

ROOT = Path(__file__).resolve().parents[1]


class ValidationNormalizationTests(unittest.TestCase):
    def test_normalizes_current_validation_shapes_without_dropping_unknowns(self) -> None:
        catalog = normalize_catalog(CATALOG_PAYLOAD)
        config = {
            "version": "1.0",
            "commands": {
                "kubernetes": {
                    "steps": [
                        {
                            "name": "setup",
                            "phase": "setup",
                            "command": "scripts/k8s/setup.sh",
                            "owner_hint": "provider",
                        },
                        {"name": "teardown", "phase": "teardown", "skip": True},
                    ]
                }
            },
            "tests": {
                "platform": "kubernetes",
                "description": "Adapter fixture",
                "validations": {
                    "kubernetes": {
                        "checks": {
                            "K8sNodeCountCheck": {"count": 3},
                        }
                    },
                    "node_pools": [
                        {
                            "K8sNodePoolCheck-gpu": {
                                "step": "create_gpu_pool",
                                "expected_replicas": 2,
                            }
                        },
                        "not-an-object",
                    ],
                    "storage": {
                        "step": "setup",
                        "phase": "setup",
                        "owner_hint": "platform",
                        "checks": [{"K8sCsiStorageTypesCheck": {}}],
                    },
                    "future_category": {
                        "FutureValidationCheck": {"enabled": True},
                        "BrokenValidationCheck": True,
                    },
                    "reframe": {"checks": {"CPUInfoCheck": {}}},
                },
                "exclude": {"labels": ["slow"]},
            },
        }

        plan = normalize_validation_plan(
            config,
            catalog,
            config_files=("provider.yaml",),
            isvctl_version="isvctl 0.8.0",
        )

        self.assertEqual(plan.contract_version, ADAPTER_CONTRACT_VERSION)
        self.assertEqual(plan.catalog_version, "0.8.0")
        self.assertEqual(plan.platform, "kubernetes")
        self.assertEqual(plan.config_files, ("provider.yaml",))
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].metadata, {"owner_hint": "provider"})
        self.assertTrue(plan.steps[1].skipped)

        by_name = {validation.name: validation for validation in plan.validations}
        node_count = by_name["K8sNodeCountCheck"]
        self.assertTrue(node_count.known_to_catalog)
        self.assertEqual(node_count.labels, ("kubernetes",))
        self.assertEqual(node_count.phase, "test")

        variant = by_name["K8sNodePoolCheck-gpu"]
        self.assertEqual(variant.base_name, "K8sNodePoolCheck")
        self.assertTrue(variant.known_to_catalog)
        self.assertEqual(variant.step, "create_gpu_pool")

        storage = by_name["K8sCsiStorageTypesCheck"]
        self.assertEqual(storage.step, "setup")
        self.assertEqual(storage.phase, "setup")
        self.assertEqual(storage.metadata["group"], {"owner_hint": "platform"})

        future = by_name["FutureValidationCheck"]
        self.assertFalse(future.known_to_catalog)
        self.assertTrue(future.valid)
        self.assertEqual(future.params, {"enabled": True})

        broken = by_name["BrokenValidationCheck"]
        self.assertFalse(broken.valid)
        self.assertIn("parameters must be an object", broken.error or "")

        invalid = [validation for validation in plan.validations if validation.name == "<invalid>"]
        self.assertEqual(len(invalid), 1)
        self.assertFalse(invalid[0].valid)
        self.assertIn("must be an object", invalid[0].error or "")

        reframe = by_name["CPUInfoCheck"]
        self.assertEqual(reframe.execution_adapter, "reframe")
        self.assertFalse(reframe.known_to_catalog)

    def test_rejects_invalid_catalog_and_preserves_invalid_config(self) -> None:
        with self.assertRaisesRegex(ValidationAdapterError, "entries.*list"):
            normalize_catalog({"entries": {}})

        catalog = normalize_catalog({"entries": []})
        plan = normalize_validation_plan({"tests": {"validations": []}}, catalog)
        self.assertEqual(len(plan.validations), 1)
        self.assertFalse(plan.validations[0].valid)
        self.assertIn("tests.validations", plan.validations[0].error or "")


class IsvctlAdapterTests(unittest.TestCase):
    def test_builds_plan_from_machine_readable_cli_contracts(self) -> None:
        calls: list[tuple[tuple[str, ...], Path | None, int]] = []
        merged_config = {
            "version": "1.0",
            "tests": {
                "platform": "kubernetes",
                "validations": {"kubernetes": {"checks": {"K8sNodeCountCheck": {}}}},
            },
        }

        def runner(
            command: Sequence[str], cwd: Path | None, timeout_seconds: int
        ) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(command), cwd, timeout_seconds))
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "isvctl 0.8.0\n", "")
            if tuple(command[-3:]) == ("catalog", "list", "--json"):
                return subprocess.CompletedProcess(command, 0, json.dumps(CATALOG_PAYLOAD), "")
            if "--dry-run" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(merged_config), "")
            raise AssertionError(f"Unexpected command: {command}")

        validation_root = Path("/tmp/ai-cloud-validation")
        adapter = IsvctlAdapter(validation_root, runner=runner, timeout_seconds=17)
        plan = adapter.plan(("provider.yaml", "override.yaml"))

        self.assertEqual(plan.isvctl_version, "isvctl 0.8.0")
        self.assertEqual(plan.catalog_version, "0.8.0")
        self.assertEqual(len(plan.validations), 1)
        schema = json.loads((ROOT / "schemas" / "validation-plan.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(plan.to_dict())
        self.assertEqual(
            calls[0][0],
            ("uv", "run", "isvctl", "--version"),
        )
        self.assertEqual(
            calls[2][0],
            (
                "uv",
                "run",
                "isvctl",
                "test",
                "run",
                "-f",
                "provider.yaml",
                "-f",
                "override.yaml",
                "--dry-run",
                "--no-upload",
            ),
        )
        self.assertEqual(calls[0][1], validation_root.resolve())
        self.assertEqual(calls[0][2], 17)

    def test_surfaces_cli_and_json_contract_failures(self) -> None:
        def failed_runner(
            command: Sequence[str], cwd: Path | None, timeout_seconds: int
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 2, "", "invalid provider configuration")

        adapter = IsvctlAdapter(executable=("isvctl",), runner=failed_runner)
        with self.assertRaisesRegex(ValidationAdapterError, "exit code 2.*invalid provider"):
            adapter.catalog()

        def invalid_json_runner(
            command: Sequence[str], cwd: Path | None, timeout_seconds: int
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "not-json", "")

        adapter = IsvctlAdapter(executable=("isvctl",), runner=invalid_json_runner)
        with self.assertRaisesRegex(ValidationAdapterError, "did not emit valid JSON"):
            adapter.catalog()

    def test_prefers_checkout_virtual_environment_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            executable = validation_root / ".venv" / "bin" / "isvctl"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            calls: list[tuple[str, ...]] = []

            def runner(
                command: Sequence[str], cwd: Path | None, timeout_seconds: int
            ) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(command))
                return subprocess.CompletedProcess(command, 0, json.dumps(CATALOG_PAYLOAD), "")

            adapter = IsvctlAdapter(validation_root, runner=runner)
            adapter.catalog()

        self.assertEqual(calls[0], (str(executable), "catalog", "list", "--json"))

    def test_cli_exports_schema_valid_validation_plan(self) -> None:
        from isv_readiness.cli import main

        catalog = normalize_catalog(CATALOG_PAYLOAD)
        plan = normalize_validation_plan(
            {
                "version": "1.0",
                "tests": {
                    "platform": "kubernetes",
                    "validations": {"kubernetes": {"checks": {"K8sNodeCountCheck": {}}}},
                },
            },
            catalog,
            config_files=("provider.yaml",),
            isvctl_version="isvctl 0.8.0",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "validation-plan.json"
            validation_root = Path(tempdir) / "ai-cloud-validation"
            with patch("isv_readiness.cli.IsvctlAdapter") as adapter_class:
                adapter_class.return_value.plan.return_value = plan
                exit_code = main(
                    [
                        "plan",
                        "-f",
                        "provider.yaml",
                        "--validation-root",
                        str(validation_root),
                        "--out",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        adapter_class.assert_called_once_with(validation_root)
        schema = json.loads((ROOT / "schemas" / "validation-plan.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)

    def test_cli_plan_allows_isvctl_from_path_without_checkout(self) -> None:
        from isv_readiness.cli import main

        plan = normalize_validation_plan(
            {"tests": {"platform": "vm", "validations": {}}},
            normalize_catalog({"entries": []}),
            config_files=("provider.yaml",),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "validation-plan.json"
            with patch("isv_readiness.cli.IsvctlAdapter") as adapter_class:
                adapter_class.return_value.plan.return_value = plan
                exit_code = main(
                    [
                        "plan",
                        "-f",
                        "provider.yaml",
                        "--out",
                        str(output),
                    ]
                )

        self.assertEqual(exit_code, 0)
        adapter_class.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
