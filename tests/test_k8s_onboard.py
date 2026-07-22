from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

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
            self.assertTrue((providers_dir / "dsx-air" / "config" / "kubernetes.yaml").exists())
            self.assertTrue((providers_dir / "dsx-air" / "scripts" / "k8s" / "setup.sh").exists())
            wrapper = (providers_dir / "dsx-air" / "config" / "kubernetes.yaml").read_text(encoding="utf-8")
            self.assertIn('command: "../scripts/k8s/setup.sh"', wrapper)
            self.assertIn("../../../suites/k8s.yaml", wrapper)

            # The automatic review workspace copies only the provider root.
            # Its Kubernetes config must survive that boundary.
            scratch = Path(tempdir) / "scratch-provider"
            shutil.copytree(plan.provider_dir, scratch)
            self.assertTrue((scratch / "config" / "kubernetes.yaml").is_file())

            scope = json.loads((providers_dir / "dsx-air" / "isv-readiness.k8s.scope.json").read_text())
            self.assertEqual(scope["provider"], "dsx-air")
            self.assertIn("needed_isv_info", scope)
            self.assertEqual(scope["owns"], {})

    def test_building_plan_does_not_write_until_explicit_application(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            (validation_root / "isvctl" / "configs" / "providers").mkdir(parents=True)

            plan = build_k8s_onboarding_plan(validation_root, "acme-k8s")
            self.assertFalse(plan.wrapper_path.exists())

            write_k8s_onboarding_files(plan)
            self.assertTrue(plan.wrapper_path.exists())


if __name__ == "__main__":
    unittest.main()
