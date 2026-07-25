from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import load_solution_profile
from isv_readiness.validation_adapter import normalize_catalog, normalize_validation_plan

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class UpstreamSuiteCompatibilityTests(unittest.TestCase):
    def test_storage_and_hyphenated_domain_files_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            suites = validation_root / "isvctl" / "configs" / "suites"
            provider = validation_root / "isvctl" / "configs" / "providers" / "acme"
            config_dir = provider / "config"
            suites.mkdir(parents=True)
            config_dir.mkdir(parents=True)

            domains = {
                "storage": ("storage.yaml", "StorageContractCheck"),
                "control_plane": ("control-plane.yaml", "ControlPlaneContractCheck"),
                "image_registry": ("image-registry.yaml", "ImageRegistryContractCheck"),
            }
            for domain, (filename, check) in domains.items():
                suite = {
                    "version": "1.0",
                    "tests": {
                        "platform": domain,
                        "validations": {"contract": {"checks": {check: {}}}},
                    },
                }
                (suites / filename).write_text(
                    yaml.safe_dump(suite, sort_keys=False),
                    encoding="utf-8",
                )
                (config_dir / filename).write_text(
                    yaml.safe_dump(
                        {
                            "import": f"../../../suites/{filename}",
                            "version": "1.0",
                            "tests": {"platform": domain},
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )

            report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=list(domains),
                    validation_root=validation_root,
                )
            )

            self.assertFalse(any(row.step_name == "<config>" for row in report.rows))
            self.assertEqual(
                {row.validation_class for row in report.rows},
                {check for _, check in domains.values()},
            )

    def test_new_upstream_validation_is_preserved_and_routed_without_mutating_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            shutil.copytree(FIXTURES / "ai-cloud-validation", validation_root)
            suite_path = validation_root / "isvctl" / "configs" / "suites" / "k8s.yaml"
            provider = validation_root / "isvctl" / "configs" / "providers" / "dsx-air"

            catalog = normalize_catalog({"isvTestVersion": "0.8.0", "entries": []})
            baseline_config = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
            baseline_plan = normalize_validation_plan(baseline_config, catalog)
            baseline_report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["k8s"],
                    validation_root=validation_root,
                )
            )

            changed_config = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
            changed_config["tests"]["validations"]["upstream_future"] = {
                "checks": {
                    "K8sFutureUpstreamCheck": {
                        "enabled": True,
                    }
                }
            }
            suite_path.write_text(
                yaml.safe_dump(changed_config, sort_keys=False),
                encoding="utf-8",
            )
            changed_suite_text = suite_path.read_text(encoding="utf-8")

            changed_plan = normalize_validation_plan(changed_config, catalog)
            changed_report = scan_provider(
                ScanOptions(
                    provider_repo=provider,
                    domains=["k8s"],
                    validation_root=validation_root,
                )
            )

            self.assertNotEqual(baseline_plan.config_fingerprint, changed_plan.config_fingerprint)
            self.assertEqual(len(changed_plan.validations), len(baseline_plan.validations) + 1)
            planned = next(
                validation for validation in changed_plan.validations if validation.name == "K8sFutureUpstreamCheck"
            )
            self.assertEqual(planned.category, "upstream_future")
            self.assertTrue(planned.valid)
            self.assertFalse(planned.known_to_catalog)

            self.assertEqual(len(changed_report.rows), len(baseline_report.rows) + 1)
            row = next(item for item in changed_report.rows if item.validation_class == "K8sFutureUpstreamCheck")
            self.assertEqual(row.status, "pass")
            self.assertEqual(row.enrichment["validation_category"], "upstream_future")
            self.assertFalse(row.enrichment["requires_provider_step"])

            profile = load_solution_profile(ROOT / "examples" / "profiles" / "bcm.reference.yaml")
            enriched = enrich_report_with_profile(changed_report, profile)
            enriched_row = next(item for item in enriched.rows if item.validation_class == "K8sFutureUpstreamCheck")
            routing = enriched_row.enrichment["solution_profile"]
            self.assertEqual(routing["capability_id"], "kubernetes.default")
            self.assertEqual(routing["action"], "implement_or_fix_adapter")

            plan_schema = load_schema("validation-plan.schema.json")
            gaps_schema = load_schema("gaps.schema.json")
            jsonschema.Draft202012Validator(plan_schema).validate(changed_plan.to_dict())
            jsonschema.Draft202012Validator(gaps_schema).validate(enriched.to_dict())

            self.assertEqual(suite_path.read_text(encoding="utf-8"), changed_suite_text)


if __name__ == "__main__":
    unittest.main()
