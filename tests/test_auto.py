from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from isv_readiness.auto import AutoWorkflowError, _park, run_auto
from isv_readiness.decision import adapter_contract_unit
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMMIT = "e" * 40


class AutoWorkflowTests(unittest.TestCase):
    def test_auto_rejects_a_draft_qualification_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            profile_path = project_path.parent / "solution-profile.yaml"
            raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            raw["solution"]["profile_status"] = "draft"
            raw["journey"] = {"stage": "qualify", "status": "in_progress"}
            profile_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(AutoWorkflowError, "profile_status.*journey stage"):
                run_auto(
                    project_path,
                    domain="vm",
                    work_dir=Path(tempdir) / "work",
                    generator_command=["fixture-generator"],
                    generator_runner=_generator_runner,
                )

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


class AutoResilienceTests(unittest.TestCase):
    def test_generator_infrastructure_failure_stops_without_identical_retries(self) -> None:
        calls = {"n": 0}

        def timed_out(command, cwd, request, environment, timeout):
            del cwd, request, environment, timeout
            calls["n"] += 1
            return subprocess.CompletedProcess(command, 124, "", "model timed out")

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            with self.assertRaisesRegex(AutoWorkflowError, "infrastructure failed"):
                run_auto(
                    project_path,
                    domain="vm",
                    work_dir=Path(tempdir) / "work",
                    generator_command=["fixture-generator"],
                    generator_runner=timed_out,
                )

            self.assertEqual(calls["n"], 1)
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())

    def test_evidence_grounded_refusal_is_parked_with_the_exact_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=_refusal_runner,
            )

        self.assertEqual(review.status, "no_changes")
        self.assertTrue(review.parked)
        self.assertTrue(
            any("direct node SSH" in item.reason for item in review.parked),
            review.parked,
        )

    def test_malformed_generation_counts_as_attempt_and_run_continues(self) -> None:
        calls = {"n": 0}
        requests = []

        def flaky_runner(command, cwd, request, environment, timeout):
            calls["n"] += 1
            requests.append(json.loads(request))
            if calls["n"] == 1:
                return subprocess.CompletedProcess(command, 0, "prose, not json", "")
            return _generator_runner(command, cwd, request, environment, timeout)

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=flaky_runner,
            )

        # First generation was garbage; the run still staged the fix on retry.
        self.assertEqual(review.status, "awaiting_review")
        self.assertTrue(review.staged)
        self.assertGreaterEqual(calls["n"], 2)
        retry_items = requests[1]["context_pack"]["items"]
        feedback = next(item for item in retry_items if item["source_id"] == "previous_attempt_feedback")
        self.assertIn("deterministic guardrail", feedback["content"])
        self.assertIn("one JSON object", feedback["content"])
        digest = json.loads(feedback["content"])
        self.assertEqual(digest["latest"]["category"], "guardrail")
        self.assertEqual(len(digest["ledger"]), 1)
        self.assertEqual(digest["latest"]["fingerprint"], digest["ledger"][0]["fingerprint"])

    def test_same_deterministic_failure_twice_parks_without_a_third_generation(self) -> None:
        calls_by_target: dict[str, int] = {}

        def always_malformed(command, cwd, request, environment, timeout):
            del cwd, environment, timeout
            payload = json.loads(request)
            target = payload["context_pack"]["gap"]["remediation"]["target"]
            calls_by_target[target] = calls_by_target.get(target, 0) + 1
            return subprocess.CompletedProcess(command, 0, "prose, not json", "")

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=always_malformed,
            )

        self.assertTrue(calls_by_target)
        self.assertTrue(all(count == 2 for count in calls_by_target.values()))
        self.assertEqual(review.status, "no_changes")
        self.assertTrue(review.parked)
        self.assertTrue(any("repeated twice" in item.reason for item in review.parked))

    def test_different_failures_reach_third_attempt_with_a_compact_ledger(self) -> None:
        calls = {"n": 0}
        requests: list[dict] = []

        def evolving_runner(command, cwd, request, environment, timeout):
            calls["n"] += 1
            requests.append(json.loads(request))
            if calls["n"] == 1:
                return subprocess.CompletedProcess(command, 0, "prose, not json", "")
            if calls["n"] == 2:
                result = _generator_runner(command, cwd, request, environment, timeout)
                output = json.loads(result.stdout)
                output["gap_id"] = "gap_ffffffffffff"
                return subprocess.CompletedProcess(command, 0, json.dumps(output), "")
            return _generator_runner(command, cwd, request, environment, timeout)

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=evolving_runner,
            )

        self.assertEqual(review.status, "awaiting_review")
        third_feedback = next(
            item
            for item in requests[2]["context_pack"]["items"]
            if item["source_id"] == "previous_attempt_feedback"
        )
        digest = json.loads(third_feedback["content"])
        self.assertEqual(len(digest["ledger"]), 2)
        self.assertNotEqual(digest["ledger"][0]["fingerprint"], digest["ledger"][1]["fingerprint"])
        self.assertIn("expected", " ".join(digest["latest"]["details"]))

    def test_retry_budget_is_shared_by_rows_with_the_same_target(self) -> None:
        rows = []
        for index in range(2):
            rows.append(
                {
                    "id": f"gap_shared{index:06d}",
                    "domain": "vm",
                    "status": "not_implemented",
                    "remediation": {
                        "auto_fixable": True,
                        "target": "scripts/vm/shared.py",
                    },
                    "enrichment": {
                        "solution_profile": {
                            "action": "implement_or_fix_adapter",
                            "owned": True,
                            "profile_status": "reviewed",
                            "journey_stage": "validate",
                        }
                    },
                }
            )

        parked = _park(
            {"rows": rows},
            "vm",
            [],
            {"scripts/vm/shared.py": 3},
            3,
            feedback_by_unit={
                "scripts/vm/shared.py": (
                    "Previous candidate was rejected by a deterministic guardrail: config scope changed.",
                )
            },
        )

        self.assertEqual(len(parked), 2)
        self.assertTrue(all("exhausted" in item.reason for item in parked))
        self.assertTrue(all("config scope changed" in item.reason for item in parked))

    def test_config_retry_budget_is_scoped_to_one_step(self) -> None:
        rows = _config_rows()

        parked = _park(
            {"rows": rows},
            "vm",
            [],
            {adapter_contract_unit(rows[0]): 3},
            3,
        )

        by_id = {item.gap_id: item for item in parked}
        self.assertIn("exhausted", by_id[rows[0]["id"]].reason)
        self.assertIn("Not attempted", by_id[rows[1]["id"]].reason)

    def test_config_generator_refusal_is_scoped_to_one_step(self) -> None:
        rows = _config_rows()
        parked = _park(
            {"rows": rows},
            "vm",
            [],
            {adapter_contract_unit(rows[0]): 3},
            3,
            blocked_by_unit={adapter_contract_unit(rows[0]): "launch interface is unavailable"},
        )

        by_id = {item.gap_id: item for item in parked}
        self.assertIn("launch interface is unavailable", by_id[rows[0]["id"]].reason)
        self.assertNotIn("launch interface is unavailable", by_id[rows[1]["id"]].reason)
        self.assertIn("Not attempted", by_id[rows[1]["id"]].reason)


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
    _ratify_profile(workspace / "solution-profile.yaml")
    raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
    raw["provider"]["path"] = "provider"
    raw["provider"]["state"] = "existing"
    plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return plan.manifest_path, provider


def _config_rows() -> list[dict]:
    rows = []
    for index, step_name in enumerate(("launch", "describe")):
        rows.append(
            {
                "id": f"gap_config{index:06d}",
                "domain": "vm",
                "step_name": step_name,
                "status": "not_implemented",
                "remediation": {
                    "auto_fixable": True,
                    "target": "config/vm.yaml",
                },
                "enrichment": {
                    "solution_profile": {
                        "action": "implement_or_fix_adapter",
                        "owned": True,
                        "profile_status": "reviewed",
                        "journey_stage": "validate",
                    }
                },
            }
        )
    return rows


def _ratify_profile(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["solution"]["profile_status"] = "reviewed"
    raw["journey"] = {"stage": "validate", "status": "ready"}
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


if __name__ == "__main__":
    unittest.main()
