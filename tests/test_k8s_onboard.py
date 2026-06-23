from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from isv_readiness.cli import main
from isv_readiness.scan.k8s_onboard import build_k8s_onboarding_plan, write_k8s_onboarding_files


class K8sOnboardingTests(unittest.TestCase):
    def test_write_k8s_onboarding_files_for_brand_new_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            providers_dir = validation_root / "isvctl" / "configs" / "providers"
            template_dir = providers_dir / "my-isv" / "scripts" / "k8s"
            template_dir.mkdir(parents=True)
            (template_dir / "setup.sh").write_text("#!/bin/sh\necho template-setup\n", encoding="utf-8")
            (template_dir / "teardown.sh").write_text("#!/bin/sh\necho template-teardown\n", encoding="utf-8")

            plan = build_k8s_onboarding_plan(validation_root, "dsx-air")
            written = write_k8s_onboarding_files(plan)

            self.assertEqual(len(written), 4)
            self.assertTrue((providers_dir / "dsx-air.yaml").exists())
            self.assertTrue((providers_dir / "dsx-air" / "scripts" / "k8s" / "setup.sh").exists())
            wrapper = (providers_dir / "dsx-air.yaml").read_text(encoding="utf-8")
            self.assertIn('command: "dsx-air/scripts/k8s/setup.sh"', wrapper)
            self.assertIn("import: ../suites/k8s.yaml", wrapper)

            scope = json.loads((providers_dir / "dsx-air" / "isv-readiness.k8s.scope.json").read_text())
            self.assertEqual(scope["provider"], "dsx-air")
            self.assertIn("needed_isv_info", scope)
            self.assertTrue(scope["owns"]["node_inventory"])

    def test_cli_onboard_dry_plan_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            (validation_root / "isvctl" / "configs" / "providers").mkdir(parents=True)

            dry_exit = main(
                [
                    "onboard",
                    "--domain",
                    "k8s",
                    "--provider-name",
                    "acme-k8s",
                    "--validation-root",
                    str(validation_root),
                ]
            )
            self.assertEqual(dry_exit, 0)
            self.assertFalse((validation_root / "isvctl" / "configs" / "providers" / "acme-k8s.yaml").exists())

            write_exit = main(
                [
                    "onboard",
                    "--domain",
                    "k8s",
                    "--provider-name",
                    "acme-k8s",
                    "--validation-root",
                    str(validation_root),
                    "--write",
                ]
            )
            self.assertEqual(write_exit, 0)
            self.assertTrue((validation_root / "isvctl" / "configs" / "providers" / "acme-k8s.yaml").exists())


if __name__ == "__main__":
    unittest.main()
