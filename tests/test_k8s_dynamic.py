from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.cli import main
from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.report import render_report

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DSX = FIXTURES / "dsx-k8s"
PROVIDER = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "dsx-air"


class K8sDynamicScanTests(unittest.TestCase):
    def test_dynamic_artifacts_emit_scope_aware_rows(self) -> None:
        rows = scan_k8s_artifacts(
            K8sDynamicArtifacts(
                provider_repo=PROVIDER,
                junit_path=DSX / "junit.xml",
                log_path=DSX / "isvctl.log",
                setup_json_path=DSX / "setup.json",
                config_path=PROVIDER.with_suffix(".yaml"),
                scope=load_k8s_scope(DSX / "scope.json"),
            )
        )
        by_validation = {row.validation_class: row for row in rows}

        self.assertEqual(by_validation["StepOutputSchema"].status, "pass")
        self.assertEqual(by_validation["K8sGpuCapacityCheck"].status, "fail")
        self.assertEqual(by_validation["K8sGpuCapacityCheck"].gap_type, "product_bug")
        self.assertEqual(by_validation["K8sGpuCapacityCheck"].enrichment["k8s_layer"], "gpu_operator")
        self.assertIn("K8sGpuCapacityCheck", by_validation["K8sGpuCapacityCheck"].evidence.stderr_excerpt or "")

        self.assertEqual(by_validation["K8sNetworkPolicyCheck"].gap_type, "semantic_mismatch")
        self.assertEqual(by_validation["K8sNimHelmWorkload-1b"].gap_type, "lab_env")
        self.assertEqual(by_validation["K8sNodePoolCheck"].step_name, "create_test_node_pool")
        self.assertEqual(by_validation["K8sNodePoolCheck"].gap_type, "semantic_mismatch")

    def test_cli_merges_static_and_dynamic_k8s_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "gaps.json"
            exit_code = main(
                [
                    "scan",
                    "-p",
                    str(PROVIDER),
                    "--domains",
                    "k8s",
                    "--validation-root",
                    str(FIXTURES / "ai-cloud-validation"),
                    "--junit",
                    str(DSX / "junit.xml"),
                    "--log",
                    str(DSX / "isvctl.log"),
                    "--setup-json",
                    str(DSX / "setup.json"),
                    "--scope",
                    str(DSX / "scope.json"),
                    "--out",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            data = json.loads(output.read_text(encoding="utf-8"))

        schema = json.loads((ROOT / "schemas" / "gaps.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        self.assertTrue(any(row["detection"] == "static" for row in data["rows"]))
        self.assertTrue(any(row["detection"] == "dynamic" for row in data["rows"]))
        self.assertIn("Readiness score", render_report(data, "scorecard"))


if __name__ == "__main__":
    unittest.main()
