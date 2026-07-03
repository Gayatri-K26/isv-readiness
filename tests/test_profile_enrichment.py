from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import jsonschema

from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import load_solution_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ProfileEnrichmentTests(unittest.TestCase):
    def test_profile_routes_k8s_capabilities_without_changing_scan_status(self) -> None:
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "dsx-air"
        report = scan_provider(
            ScanOptions(
                provider_repo=provider,
                domains=["k8s"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        profile = load_solution_profile(ROOT / "examples" / "profiles" / "bcm.reference.yaml")

        enriched = enrich_report_with_profile(report, profile)
        by_check = {(row.step_name, row.validation_class): row for row in enriched.rows}

        node_count = by_check[("<validation>", "K8sNodeCountCheck")]
        node_profile = node_count.enrichment["solution_profile"]
        self.assertEqual(node_count.status, "pass")
        self.assertEqual(node_profile["action"], "implement_or_fix_adapter")
        self.assertEqual(node_profile["capability_id"], "kubernetes.default")

        storage = by_check[("inspect_storage", "K8sCsiStorageTypesCheck")]
        storage_profile = storage.enrichment["solution_profile"]
        self.assertEqual(storage.status, "not_implemented")
        self.assertEqual(storage_profile["capability_id"], "bcm-k8s-storage")
        self.assertEqual(storage_profile["action"], "request_scope_decision")
        self.assertFalse(storage.remediation.auto_fixable)

        schema = json.loads((ROOT / "schemas" / "gaps.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(enriched.to_dict())

    def test_out_of_scope_domain_disables_automatic_adapter_edits(self) -> None:
        report = scan_provider(
            ScanOptions(
                provider_repo=FIXTURES / "provider_repo",
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        profile = load_solution_profile(ROOT / "examples" / "profiles" / "bcm.reference.yaml")

        enriched = enrich_report_with_profile(report, profile)

        self.assertTrue(any(row.remediation.auto_fixable for row in report.rows))
        self.assertFalse(any(row.remediation.auto_fixable for row in enriched.rows))
        self.assertEqual(
            {row.enrichment["solution_profile"]["action"] for row in enriched.rows},
            {"skip_with_rationale"},
        )

    def test_masked_failure_on_owned_domain_is_rerouted_to_scope_decision(self) -> None:
        from dataclasses import replace

        report = scan_provider(
            ScanOptions(
                provider_repo=FIXTURES / "provider_repo",
                domains=["vm"],
                validation_root=FIXTURES / "ai-cloud-validation",
            )
        )
        profile = load_solution_profile(ROOT / "examples" / "profiles" / "bcm.reference.yaml")
        # The operator misidentifies vm as partner-owned/out-of-scope when it is
        # actually the ISV's: flip only the ownership assertion, leave the failing
        # scan untouched.
        vm_index = next(i for i, d in enumerate(profile.domains) if d.domain == "vm")
        owned_vm = replace(profile.domains[vm_index], owned=True)
        misidentified = replace(
            profile,
            domains=profile.domains[:vm_index] + (owned_vm,) + profile.domains[vm_index + 1 :],
        )

        enriched = enrich_report_with_profile(report, misidentified)
        failing = [row for row in enriched.rows if row.status in {"fail", "not_implemented", "error"}]
        self.assertTrue(failing, "expected at least one failing vm row in the fixture")
        for row in failing:
            sp = row.enrichment["solution_profile"]
            self.assertTrue(sp["owned"])
            self.assertEqual(sp["action"], "request_scope_decision")
            self.assertTrue(sp["reconciliation"]["masked_failure"])
            self.assertEqual(sp["reconciliation"]["original_action"], "skip_with_rationale")

        # A genuinely un-owned domain must NOT be reconciled away.
        unchanged = enrich_report_with_profile(report, profile)
        for row in unchanged.rows:
            sp = row.enrichment["solution_profile"]
            self.assertFalse(sp["owned"])
            self.assertNotIn("reconciliation", sp)
            self.assertEqual(sp["action"], "skip_with_rationale")

    def test_cli_profile_summary_and_profile_aware_scan(self) -> None:
        from isv_readiness.cli import main

        profile_path = ROOT / "examples" / "profiles" / "bcm.reference.yaml"
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["profile", "--in", str(profile_path)])

        self.assertEqual(exit_code, 0)
        self.assertIn("Validation-ready (owned scope): no", stdout.getvalue())
        self.assertIn("Owned domains: bare_metal, kubernetes, observability, slurm", stdout.getvalue())
        self.assertIn("bcm-k8s-identity", stdout.getvalue())

        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "dsx-air"
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "profile-gaps.json"
            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "scan",
                        "-p",
                        str(provider),
                        "--domains",
                        "k8s",
                        "--validation-root",
                        str(FIXTURES / "ai-cloud-validation"),
                        "--profile",
                        str(profile_path),
                        "--out",
                        str(output),
                    ]
                )
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        storage = next(row for row in data["rows"] if row["validation_class"] == "K8sCsiStorageTypesCheck")
        self.assertEqual(storage["enrichment"]["solution_profile"]["action"], "request_scope_decision")

    def test_cli_derives_scan_domains_from_profile(self) -> None:
        from isv_readiness.cli import main

        profile_path = ROOT / "examples" / "profiles" / "bcm.reference.yaml"
        provider = FIXTURES / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "dsx-air"
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "all-covered-domains.json"
            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "scan",
                        "-p",
                        str(provider),
                        "--validation-root",
                        str(FIXTURES / "ai-cloud-validation"),
                        "--profile",
                        str(profile_path),
                        "--out",
                        str(output),
                    ]
                )
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            data["domains"],
            ["bare_metal", "kubernetes", "slurm", "observability"],
        )


if __name__ == "__main__":
    unittest.main()
