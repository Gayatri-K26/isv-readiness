from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from isv_readiness.solution_profile import (
    PROFILE_SCHEMA_VERSION,
    SolutionProfileError,
    load_solution_profile,
    parse_solution_profile,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "examples" / "profiles"


class SolutionProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bcm_path = PROFILES / "bcm.reference.yaml"
        self.nmc_path = PROFILES / "nvidia-mission-control.reference.yaml"

    def test_reference_profiles_scope_summary_covers_owned_domains_only(self) -> None:
        bcm = load_solution_profile(self.bcm_path)
        nmc = load_solution_profile(self.nmc_path)

        self.assertEqual(bcm.schema_version, PROFILE_SCHEMA_VERSION)
        self.assertEqual(bcm.solution.profile_status, "draft")
        self.assertEqual(bcm.journey.stage, "qualify")
        self.assertEqual(len(bcm.domains), 10)
        self.assertEqual(len(nmc.domains), 10)

        # Readiness assesses only the ISV-owned domains; external-dependency
        # domains owned by the deployment partner are excluded from the scope.
        bcm_summary = bcm.scope_summary()
        self.assertEqual(
            bcm_summary["owned_domains"],
            ["bare_metal", "kubernetes", "observability", "slurm"],
        )
        self.assertEqual(bcm_summary["coverage"]["covered"], 4)
        self.assertEqual(bcm_summary["blocking_domains"], [])
        # Owned domains are covered, but partner-supplied capabilities inside them
        # still block until resolved, so the owned scope is not yet validation-ready.
        self.assertFalse(bcm_summary["validation_ready"])
        self.assertIn("bcm-k8s-storage", bcm_summary["blocking_capabilities"])
        self.assertIn("bcm-k8s-identity", bcm_summary["blocking_capabilities"])

        nmc_summary = nmc.scope_summary()
        self.assertEqual(
            nmc_summary["owned_domains"],
            ["bare_metal", "kubernetes", "observability", "slurm"],
        )
        self.assertFalse(nmc_summary["validation_ready"])
        self.assertIn("nmc-attestation-boundary", nmc_summary["blocking_capabilities"])

    def test_resolves_domain_defaults_and_capability_overrides(self) -> None:
        bcm = load_solution_profile(self.bcm_path)

        lifecycle = bcm.resolve(
            "bare_metal",
            step_name="launch_instance",
            validation_category="setup_checks",
            validation_class="InstanceStateCheck",
        )
        self.assertIsNotNone(lifecycle)
        assert lifecycle is not None
        self.assertEqual(lifecycle.action, "implement_or_fix_adapter")
        self.assertEqual(lifecycle.capability_id, "bare_metal.default")
        self.assertEqual(lifecycle.provider_adapter_owner_actor_id, "bcm-isv")

        storage = bcm.resolve(
            "k8s",
            validation_category="k8s_storage",
            validation_class="K8sCsiStorageTypesCheck",
        )
        self.assertIsNotNone(storage)
        assert storage is not None
        self.assertEqual(storage.domain, "kubernetes")
        self.assertEqual(storage.capability_id, "bcm-k8s-storage")
        self.assertEqual(storage.coverage, "unknown")
        self.assertEqual(storage.action, "request_scope_decision")
        self.assertEqual(storage.provider_adapter_owner_actor_id, "deployment-partner")

        vm = bcm.resolve("vm", validation_class="InstanceStateCheck")
        self.assertIsNotNone(vm)
        assert vm is not None
        self.assertEqual(vm.action, "skip_with_rationale")
        self.assertIsNone(bcm.resolve("not-a-domain"))

    def test_mission_control_recovery_and_observability_are_testable(self) -> None:
        nmc = load_solution_profile(self.nmc_path)

        recovery = nmc.resolve(
            "bare_metal",
            validation_category="host_health",
            validation_class="HostHealthCheck",
        )
        self.assertIsNotNone(recovery)
        assert recovery is not None
        self.assertEqual(recovery.capability_id, "nmc-autonomous-recovery")
        self.assertEqual(recovery.component_ids, ("nmc-resiliency",))
        self.assertEqual(recovery.action, "implement_or_fix_adapter")

        observability = nmc.resolve("observability", validation_class="LogAvailabilityCheck")
        self.assertIsNotNone(observability)
        assert observability is not None
        self.assertEqual(observability.action, "implement_or_fix_adapter")
        self.assertIn("nmc-observability", observability.component_ids)

    def test_external_adapter_and_evidence_actions_are_distinct(self) -> None:
        payload = self._load_payload(self.bcm_path)
        network = next(item for item in payload["domains"] if item["domain"] == "network")
        network["coverage"] = "covered"
        network["validation_mode"] = "test"
        network["rationale"] = "External network is selected and ready for API validation."

        profile = parse_solution_profile(payload)
        responsibility = profile.resolve("network", validation_class="NetworkCreatedCheck")
        self.assertIsNotNone(responsibility)
        assert responsibility is not None
        self.assertEqual(responsibility.action, "request_external_adapter")

        network["validation_mode"] = "evidence"
        profile = parse_solution_profile(payload)
        responsibility = profile.resolve("network", validation_class="NetworkCreatedCheck")
        self.assertIsNotNone(responsibility)
        assert responsibility is not None
        self.assertEqual(responsibility.action, "collect_evidence")

    def test_rejects_bad_references_missing_rationale_and_ambiguous_overrides(self) -> None:
        payload = self._load_payload(self.bcm_path)
        payload["components"][0]["supplier_actor_id"] = "missing-actor"
        with self.assertRaisesRegex(SolutionProfileError, "Unknown component.*actor"):
            parse_solution_profile(payload)

        payload = self._load_payload(self.bcm_path)
        payload["components"][0]["depends_on"] = ["bcm-kubernetes"]
        with self.assertRaisesRegex(SolutionProfileError, "dependency cycle"):
            parse_solution_profile(payload)

        payload = self._load_payload(self.bcm_path)
        network = next(item for item in payload["domains"] if item["domain"] == "network")
        network["rationale"] = ""
        with self.assertRaisesRegex(SolutionProfileError, "requires a rationale"):
            parse_solution_profile(payload)

        payload = self._load_payload(self.bcm_path)
        kubernetes = next(item for item in payload["domains"] if item["domain"] == "kubernetes")
        duplicate = copy.deepcopy(kubernetes["capabilities"][0])
        duplicate["id"] = "ambiguous-storage-override"
        kubernetes["capabilities"].append(duplicate)
        profile = parse_solution_profile(payload)
        with self.assertRaisesRegex(SolutionProfileError, "Ambiguous responsibility"):
            profile.resolve(
                "kubernetes",
                validation_category="k8s_storage",
                validation_class="K8sCsiStorageTypesCheck",
            )

    @staticmethod
    def _load_payload(path: Path) -> dict:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        return payload


if __name__ == "__main__":
    unittest.main()
