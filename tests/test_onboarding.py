from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from isv_readiness.onboarding import (
    OnboardingError,
    build_provider_onboarding_plan,
    execute_provider_onboarding,
)
from isv_readiness.solution_profile import load_solution_profile

ROOT = Path(__file__).resolve().parents[1]


class CrossDomainOnboardingTests(unittest.TestCase):
    def test_scaffolds_selected_domains_and_completes_k8s_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            template_dir = validation_root / "isvctl" / "configs" / "providers" / "my-isv"
            template_dir.mkdir(parents=True)
            profile = load_solution_profile(ROOT / "examples" / "profiles" / "bcm.reference.yaml")
            plan = build_provider_onboarding_plan(
                validation_root,
                "acme",
                ["vm", "k8s"],
                profile=profile,
            )
            calls: list[tuple[tuple[str, ...], Path, int]] = []

            def runner(command: Sequence[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
                calls.append((tuple(command), cwd, timeout_seconds))
                config_dir = plan.provider_dir / "config"
                scripts_dir = plan.provider_dir / "scripts" / "k8s"
                config_dir.mkdir(parents=True)
                scripts_dir.mkdir(parents=True)
                (config_dir / "vm.yaml").write_text("tests:\n  platform: vm\n", encoding="utf-8")
                (scripts_dir / "setup.sh").write_text("#!/bin/sh\necho preserved\n", encoding="utf-8")
                (scripts_dir / "teardown.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "created", "")

            written = execute_provider_onboarding(plan, runner=runner)

            self.assertEqual(plan.domains, ("vm", "kubernetes"))
            self.assertEqual(calls[0][0], ("uv", "run", "isvctl", "provider", "scaffold", "acme"))
            self.assertEqual(calls[0][1], validation_root.resolve())
            self.assertIn(plan.provider_dir / "config" / "vm.yaml", written)
            self.assertIsNotNone(plan.k8s_plan)
            assert plan.k8s_plan is not None
            self.assertIn(plan.k8s_plan.wrapper_path, written)
            self.assertTrue((plan.provider_dir.parent / "acme.yaml").exists())
            self.assertTrue((plan.provider_dir / "isv-readiness.k8s.scope.json").exists())
            self.assertEqual(
                (plan.provider_dir / "scripts" / "k8s" / "setup.sh").read_text(encoding="utf-8"),
                "#!/bin/sh\necho preserved\n",
            )
            self.assertIn("BCM cluster and node-pool API access", plan.required_inputs["kubernetes"])

    def test_slurm_onboarding_supplies_the_missing_wrapper_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            template_dir = validation_root / "isvctl" / "configs" / "providers" / "my-isv"
            template_dir.mkdir(parents=True)
            plan = build_provider_onboarding_plan(validation_root, "acme", ["slurm"])

            def runner(command: Sequence[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
                del cwd, timeout_seconds
                # The upstream scaffold ships slurm scripts but no config.
                scripts_dir = plan.provider_dir / "scripts" / "slurm"
                scripts_dir.mkdir(parents=True, exist_ok=True)
                (scripts_dir / "setup.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                (scripts_dir / "teardown.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "created", "")

            written = execute_provider_onboarding(plan, runner=runner)

            wrapper = plan.provider_dir / "config" / "slurm.yaml"
            self.assertIn(wrapper, written)
            text = wrapper.read_text(encoding="utf-8")
            self.assertIn("../../../suites/slurm.yaml", text)
            self.assertIn("../scripts/slurm/setup.sh", text)
            self.assertIn("platform: slurm", text)
            self.assertIn(f"Slurm wrapper completion: {wrapper}", plan.summary_lines())

            # A hand-authored config is preserved on rerun without overwrite.
            wrapper.write_text("tests:\n  platform: slurm\n", encoding="utf-8")
            execute_provider_onboarding(plan, runner=runner, overwrite=False)
            self.assertEqual(wrapper.read_text(encoding="utf-8"), "tests:\n  platform: slurm\n")

    def test_uses_existing_checkout_executable_and_validates_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            (validation_root / "isvctl" / "configs" / "providers" / "my-isv").mkdir(parents=True)
            executable = validation_root / ".venv" / "bin" / "isvctl"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")

            plan = build_provider_onboarding_plan(validation_root, "acme", ["bare-metal"])
            self.assertEqual(plan.scaffold_command[0], str(executable.resolve()))
            self.assertEqual(plan.domains, ("bare_metal",))

            with self.assertRaisesRegex(OnboardingError, "Unsupported onboarding domains"):
                build_provider_onboarding_plan(validation_root, "acme", ["unknown"])
            with self.assertRaisesRegex(OnboardingError, "Provider name"):
                build_provider_onboarding_plan(validation_root, "../acme", ["vm"])

    def test_plan_summarizes_cross_domain_scaffold_and_k8s_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "ai-cloud-validation"
            (validation_root / "isvctl" / "configs" / "providers" / "my-isv").mkdir(parents=True)
            plan = build_provider_onboarding_plan(validation_root, "acme", ["vm", "k8s"])
            output = "\n".join(plan.summary_lines())

        self.assertIn("uv run isvctl provider scaffold acme", output)
        self.assertIn("Domains: vm, kubernetes", output)
        self.assertIn("Kubernetes wrapper completion", output)


if __name__ == "__main__":
    unittest.main()
