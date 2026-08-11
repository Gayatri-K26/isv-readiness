from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from isv_readiness.agent import run_agent_turn
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap
from isv_readiness.schema import load_schema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMMIT = "e" * 40


class AgentWorkflowTests(unittest.TestCase):
    def test_agent_blocks_on_an_evidence_grounded_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=_refusal_runner,
            )

            self.assertEqual(state.status, "blocked")
            self.assertIn("direct node SSH", state.reason)
            self.assertTrue(Path(state.artifacts["changes"]).exists())

    def test_agent_stops_for_generator_then_review(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"
            state = run_agent_turn(project_path, domain="vm", work_dir=work)
            self.assertEqual(state.status, "awaiting_generator")
            self.assertTrue(Path(state.artifacts["context"]).exists())

            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )
            self.assertEqual(state.status, "awaiting_review")
            self.assertIsNotNone(state.patch_sha256)
            self.assertTrue(Path(state.artifacts["patch"]).exists())
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())

            unchanged = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                approval_patch_sha256="0" * 64,
                apply_changes=True,
            )
            self.assertEqual(unchanged.status, "awaiting_review")
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())

    def test_reviewed_agent_change_requires_targeted_then_full_live_success(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"
            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )
            assert state.patch_sha256 is not None

            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
                approval_patch_sha256=state.patch_sha256,
                apply_changes=True,
                run_live=True,
                live_runner=_live_runner,
                commit_resolver=lambda root: COMMIT,
                environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "secret"},
            )
            self.assertEqual(state.status, "awaiting_live")
            self.assertIsNone(state.selected_gap_id)
            self.assertNotIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())

            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                run_live=True,
                live_runner=_live_runner,
                commit_resolver=lambda root: COMMIT,
                environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "secret"},
            )
            self.assertEqual(state.status, "complete")
            schema = load_schema("agent-state.schema.json")
            jsonschema.validate(state.to_dict(), schema)

    def test_live_failure_without_editable_evidence_is_parked_with_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"
            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )
            assert state.patch_sha256 is not None

            state = run_agent_turn(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
                approval_patch_sha256=state.patch_sha256,
                apply_changes=True,
                run_live=True,
                live_runner=_failing_live_runner,
                commit_resolver=lambda root: COMMIT,
                environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "secret"},
            )

            self.assertEqual(state.status, "blocked")
            self.assertIn("not currently evidenced as fixable", state.reason)
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())
            envelope = state.feedback[-1]
            assert isinstance(envelope, dict)
            self.assertFalse(envelope["retryable"])
            self.assertIn("provider returned 409", envelope["stable_error"])
            self.assertTrue(envelope["artifact_refs"])
            for reference in envelope["artifact_refs"]:
                path = Path(reference["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), reference["sha256"])
            jsonschema.validate(state.to_dict(), load_schema("agent-state.schema.json"))


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
    describe_script = provider / "scripts" / "vm" / "describe_instance.py"
    describe_script.write_text(
        'import json\n\nprint(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-1", "state": "running"}))\n',
        encoding="utf-8",
    )
    config = provider / "config" / "vm.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "      - name: list_instances\n",
            "      - name: describe_instance\n"
            "        phase: test\n"
            '        command: "python ../scripts/vm/describe_instance.py"\n'
            "        timeout: 60\n\n"
            "      - name: list_instances\n",
        ),
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
    _ratify_profile(workspace / "solution-profile.yaml")
    raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
    raw["provider"]["path"] = "provider"
    raw["provider"]["state"] = "existing"
    raw["execution"]["allow_live_runs"] = True
    plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return plan.manifest_path, provider


def _ratify_profile(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["solution"]["profile_status"] = "reviewed"
    raw["journey"] = {"stage": "validate", "status": "ready"}
    raw["domains"][0]["capabilities"] = [
        {
            "id": "teardown-excluded",
            "name": "Teardown excluded in this fixture",
            "selectors": {"steps": ["teardown"]},
            "coverage": "out_of_scope",
            "validation_mode": "skip",
            "rationale": "The fixture does not create cloud resources during validation.",
        }
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


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


def _refusal_runner(command, cwd, request, environment, timeout):
    del cwd, environment, timeout
    payload = json.loads(request)
    output = {
        "schema_version": "0.1.0",
        "gap_id": payload["context_pack"]["gap"]["id"],
        "context_pack_sha256": payload["context_pack_sha256"],
        "generator": {"adapter": "fixture", "model": "fixture-model"},
        "summary": "The provider exposes only a jump host, but the pinned check requires direct node SSH.",
        "changes": [],
    }
    return subprocess.CompletedProcess(command, 0, json.dumps(output), "")


def _live_runner(command, cwd, environment, timeout):
    del cwd, environment, timeout
    junit = Path(command[command.index("--junitxml") + 1])
    cases = (
        '<testcase name="test_vm[InstanceCreatedCheck]" />'
        if "-k" in command
        else (
            '<testcase name="test_vm[InstanceCreatedCheck]" />'
            '<testcase name="test_vm[InstanceListCheck]" />'
            '<testcase name="test_vm[InstanceStateCheck]" />'
            '<testcase name="test_vm[StepSuccessCheck]" />'
        )
    )
    junit.write_text(
        f"<testsuite>{cases}</testsuite>",
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0, "PASS\n", "")


def _failing_live_runner(command, cwd, environment, timeout):
    del cwd, environment, timeout
    junit = Path(command[command.index("--junitxml") + 1])
    junit.write_text(
        "<testsuite><testcase name=\"test_vm[InstanceCreatedCheck]\">"
        "<failure type=\"runtime_exception\" "
        "message=\"provider returned 409 at 2026-07-29T12:01:02Z\" />"
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 1, "InstanceCreatedCheck provider returned 409\n", "")


if __name__ == "__main__":
    unittest.main()
