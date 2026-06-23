from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from isv_readiness.scan.k8s_scope import K8sScope, classify_k8s_gap, layer_for_validation, load_k8s_scope


class K8sScopeTests(unittest.TestCase):
    def test_layer_mapping_for_common_k8s_validations(self) -> None:
        self.assertEqual(layer_for_validation("K8sGpuCapacityCheck"), "gpu_operator")
        self.assertEqual(layer_for_validation("K8sNetworkPolicyCheck"), "network_policy")
        self.assertEqual(layer_for_validation("K8sCsiStorageTypesCheck"), "storage_csi")
        self.assertEqual(layer_for_validation("K8sNimHelmWorkload-1b"), "workloads")

    def test_gpu_failure_routes_to_product_bug_when_gpu_layer_owned(self) -> None:
        result = classify_k8s_gap(
            "K8sGpuCapacityCheck",
            "fail",
            "No 'nvidia.com/gpu' resources found in node capacity",
            K8sScope(owns={"gpu_operator": True}),
        )
        self.assertEqual(result.gap_type, "product_bug")
        self.assertEqual(result.layer, "gpu_operator")
        self.assertFalse(result.auto_fixable)

    def test_network_policy_failure_routes_to_semantic_mismatch_when_out_of_scope(self) -> None:
        result = classify_k8s_gap(
            "K8sNetworkPolicyCheck",
            "fail",
            "NetworkPolicy did not take effect within 30s",
            K8sScope(owns={"network_policy": False}),
        )
        self.assertEqual(result.gap_type, "semantic_mismatch")
        self.assertEqual(result.layer, "network_policy")

    def test_missing_ngc_key_skip_routes_to_lab_env(self) -> None:
        result = classify_k8s_gap(
            "K8sNimHelmWorkload-1b",
            "skipped",
            "NGC_API_KEY not set - NGC credentials required",
            K8sScope(owns={"workloads": True}),
        )
        self.assertEqual(result.gap_type, "lab_env")
        self.assertEqual(result.layer, "workloads")

    def test_expected_skip_routes_to_semantic_mismatch(self) -> None:
        result = classify_k8s_gap(
            "K8sGpuLabelsCheck",
            "skipped",
            "No GPU nodes found",
            K8sScope(expected_skips=["K8sGpuLabelsCheck"]),
        )
        self.assertEqual(result.gap_type, "semantic_mismatch")

    def test_load_scope_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "scope.json"
            path.write_text(
                json.dumps(
                    {
                        "provider": "dsx-air",
                        "owns": {"gpu_operator": True, "network_policy": False},
                        "expected_skips": ["K8sNimHelmWorkload-1b"],
                        "run_env": "dsx-air-admin-node",
                    }
                ),
                encoding="utf-8",
            )
            scope = load_k8s_scope(path)

        self.assertEqual(scope.provider, "dsx-air")
        self.assertTrue(scope.owns["gpu_operator"])
        self.assertFalse(scope.owns["network_policy"])
        self.assertEqual(scope.expected_skips, ["K8sNimHelmWorkload-1b"])
        self.assertEqual(scope.run_env, "dsx-air-admin-node")


if __name__ == "__main__":
    unittest.main()
