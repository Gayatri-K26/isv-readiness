from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from isv_readiness.auto import run_auto
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMMIT = "e" * 40


class AutoWorkflowTests(unittest.TestCase):
    def test_auto_stages_all_fixes_and_stops_at_one_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"

            review = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )

            self.assertEqual(review.status, "awaiting_review")
            self.assertTrue(review.staged, "expected at least one staged fix")
            self.assertTrue(review.changed_files)
            # The real provider is untouched until an explicit apply.
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())
            self.assertTrue((work / "auto-review.patch").exists())

    def test_auto_apply_requires_matching_hash_then_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"

            staged = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )

            # Wrong hash is refused; provider stays untouched.
            refused = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
                apply=True,
                approval_patch_sha256="0" * 64,
            )
            self.assertEqual(refused.status, "awaiting_review")
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())

            applied = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
                apply=True,
                approval_patch_sha256=staged.patch_sha256,
            )
            self.assertEqual(applied.status, "applied")
            self.assertNotIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())
            self.assertTrue(any((work / "backups").iterdir()))


def _project(root: Path) -> tuple[Path, Path]:
    workspace = root / "workspace"
    checkout = workspace / "ai-cloud-validation"
    shutil.copytree(FIXTURES / "ai-cloud-validation", checkout)
    (checkout / ".git").mkdir()
    (checkout / "isvctl" / "configs" / "providers" / "my-isv").mkdir()
    provider = workspace / "provider"
    shutil.copytree(FIXTURES / "provider_repo", provider)
    list_script = provider / "scripts" / "vm" / "list_instances.py"
    list_script.write_text(
        'import json\n\nprint(json.dumps({"success": True, "platform": "fixture", "instances": []}))\n',
        encoding="utf-8",
    )
    plan = build_bootstrap_plan(
        workspace,
        provider_name="acme",
        domains=["vm"],
        validation_root=checkout,
        auth_env=["ACME_TOKEN"],
    )
    execute_bootstrap(plan, runner=_git_runner)
    raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
    raw["provider"]["path"] = "provider"
    raw["provider"]["state"] = "existing"
    plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return plan.manifest_path, provider


def _git_runner(command, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


def _generator_runner(command, cwd, request, environment, timeout):
    del cwd, environment, timeout
    payload = json.loads(request)
    gap = payload["context_pack"]["gap"]
    content = (
        "import json\n\n"
        'print(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-agent"}))\n'
    )
    output = {
        "schema_version": "0.1.0",
        "gap_id": gap["id"],
        "context_pack_sha256": payload["context_pack_sha256"],
        "generator": {"adapter": "fixture", "model": "fixture-model"},
        "summary": "Implement the selected VM provider stub",
        "changes": [
            {
                "target_root": "provider",
                "path": gap["remediation"]["target"],
                "operation": "replace",
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "rationale": "Return the output required by the installed validation contract",
            }
        ],
    }
    return subprocess.CompletedProcess(command, 0, json.dumps(output), "")


if __name__ == "__main__":
    unittest.main()
