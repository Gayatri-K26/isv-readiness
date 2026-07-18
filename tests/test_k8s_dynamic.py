from __future__ import annotations

import unittest
from pathlib import Path

import jsonschema

from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.report import render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema

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
        self.assertEqual(by_validation["K8sGpuCapacityCheck"].enrichment["k8s_layer"], "gpu_operator")
        self.assertIn("K8sGpuCapacityCheck", by_validation["K8sGpuCapacityCheck"].evidence.stderr_excerpt or "")

        self.assertEqual(by_validation["K8sNetworkPolicyCheck"].enrichment["k8s_layer"], "network_policy")
        self.assertEqual(by_validation["K8sNimHelmWorkload-1b"].enrichment["k8s_layer"], "workloads")
        self.assertEqual(by_validation["K8sNodePoolCheck"].step_name, "create_test_node_pool")
        self.assertEqual(by_validation["K8sNodePoolCheck"].enrichment["junit_reason"], "step_not_configured")

    def test_combined_static_and_dynamic_k8s_rows_match_gap_schema(self) -> None:
        static = scan_provider(
            ScanOptions(
                provider_repo=PROVIDER,
                domains=["kubernetes"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        dynamic = scan_k8s_artifacts(
            K8sDynamicArtifacts(
                provider_repo=PROVIDER,
                junit_path=DSX / "junit.xml",
                log_path=DSX / "isvctl.log",
                setup_json_path=DSX / "setup.json",
                config_path=PROVIDER.with_suffix(".yaml"),
                scope=load_k8s_scope(DSX / "scope.json"),
                static_rows=tuple(static.rows),
            )
        )
        data = static.to_dict()
        data["rows"].extend(row.to_dict() for row in dynamic)

        schema = load_schema("gaps.schema.json")
        jsonschema.Draft202012Validator(schema).validate(data)
        self.assertTrue(any(row["detection"] == "static" for row in data["rows"]))
        self.assertTrue(any(row["detection"] == "dynamic" for row in data["rows"]))
        self.assertIn("Readiness score", render_report(data, "scorecard"))


if __name__ == "__main__":
    unittest.main()
