from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from isv_readiness.auto import (
    AutoWorkflowError,
    ChangedFile,
    _apply_to_provider,
    _park,
    _select_fixable,
    run_auto,
)
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

    def test_domain_audit_finds_existing_scaffold_then_confirms_generated_lifecycle(self) -> None:
        calls: list[str] = []

        def lifecycle_runner(command, cwd, request, environment, timeout):
            del cwd, environment, timeout
            payload = json.loads(request)
            if "audit_context" in payload:
                calls.append("audit")
                self.assertNotIn("scanner_report", payload["audit_context"])
                self.assertNotIn("provider_sources", payload["audit_context"])
                source_ref = next(
                    item
                    for item in payload["audit_context"]["provider_paths"]
                    if item["path"] == "scripts/vm/launch_instance.py"
                )
                source = next(
                    item
                    for item in payload["audit_context"]["context_pack"]["items"]
                    if item["source_id"] == source_ref["source_id"]
                )
                implemented = "create_provider_resource" in source["content"]
                return _audit_result(
                    command,
                    payload,
                    status="implemented" if implemented else "gap",
                    target=None if implemented else "scripts/vm/launch_instance.py",
                )
            calls.append("generation")
            gap = payload["context_pack"]["gap"]
            self.assertEqual(gap["validation_class"], "DomainLifecycleAudit")
            content = (
                "import json\n\n"
                "def create_provider_resource():\n"
                "    return 'vm-agent'\n\n"
                "print(json.dumps({'success': True, 'platform': 'fixture', "
                "'instance_id': create_provider_resource()}))\n"
            )
            output = {
                "schema_version": "0.1.0",
                "gap_id": gap["id"],
                "context_pack_sha256": payload["context_pack_sha256"],
                "generator": {"adapter": "fixture", "model": "fixture-model"},
                "summary": "Replace the inventory scaffold with the approved create lifecycle",
                "changes": [
                    {
                        "target_root": "provider",
                        "path": gap["remediation"]["target"],
                        "operation": "replace",
                        "content": content,
                        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "rationale": "Perform the reviewed setup effect",
                    }
                ],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            launch = provider / "scripts" / "vm" / "launch_instance.py"
            launch.write_text(
                "import json\n\n"
                "# Existing inventory scaffold with schema-valid output.\n"
                "print(json.dumps({'success': True, 'platform': 'fixture', 'instance_id': 'existing'}))\n",
                encoding="utf-8",
            )
            describe = provider / "scripts" / "vm" / "describe_instance.py"
            describe.write_text(
                "import json\n\n"
                "print(json.dumps({'success': True, 'platform': 'fixture', "
                "'instance_id': 'existing', 'state': 'running'}))\n",
                encoding="utf-8",
            )
            config_path = provider / "config" / "vm.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            steps = config["commands"]["vm"]["steps"]
            steps.insert(
                2,
                {
                    "name": "describe_instance",
                    "phase": "test",
                    "command": "python ../scripts/vm/describe_instance.py",
                    "timeout": 60,
                },
            )
            steps[-1].pop("skip", None)
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            work = Path(tempdir) / "work"

            review = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=lifecycle_runner,
            )

            self.assertEqual(calls, ["audit", "generation", "audit"])
            self.assertEqual(review.status, "awaiting_review")
            self.assertEqual(review.staged[0].validation_class, "DomainLifecycleAudit")
            self.assertTrue((work / "domain-audit.pre.json").is_file())
            self.assertTrue((work / "domain-audit.post.json").is_file())
            staged_launch = work / "scratch-provider" / "scripts" / "vm" / "launch_instance.py"
            self.assertIn("create_provider_resource", staged_launch.read_text())
            self.assertNotIn("create_provider_resource", launch.read_text())

    def test_domain_audit_is_not_scheduled_for_out_of_scope_domain(self) -> None:
        calls = 0

        def unexpected_runner(command, cwd, request, environment, timeout):
            del command, cwd, request, environment, timeout
            nonlocal calls
            calls += 1
            raise AssertionError("out-of-scope domains must not invoke the generator")

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            profile_path = project_path.parent / "solution-profile.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            domain = next(item for item in profile["domains"] if item["domain"] == "vm")
            domain["coverage"] = "out_of_scope"
            domain["validation_mode"] = "skip"
            for capability in domain.get("capabilities", []):
                capability["coverage"] = "out_of_scope"
                capability["validation_mode"] = "skip"
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=unexpected_runner,
            )

            self.assertEqual(calls, 0)
            self.assertEqual(review.status, "no_changes")

    def test_no_changes_parks_when_execution_environment_is_not_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            raw = yaml.safe_load(project_path.read_text(encoding="utf-8"))
            raw["execution"]["run_environment"] = "not_configured"
            project_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            work = Path(tempdir) / "work"

            staged = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )
            run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
                apply=True,
                approval_patch_sha256=staged.patch_sha256,
            )
            terminal = run_auto(
                project_path,
                domain="vm",
                work_dir=work,
                generator_command=["fixture-generator"],
                generator_runner=_generator_runner,
            )

        self.assertEqual(terminal.status, "no_changes")
        preflight = next(item for item in terminal.parked if item.gap_id == "execution-preflight")
        self.assertIn("command availability", preflight.reason)

    def test_file_exchange_can_stop_after_one_imported_generator_response(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            calls = 0

            def one_response(command, cwd, request, environment, timeout):
                nonlocal calls
                calls += 1
                return _generator_runner(command, cwd, request, environment, timeout)

            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["file-exchange"],
                generator_runner=one_response,
                max_generator_calls=1,
            )

            self.assertEqual(calls, 1)
            self.assertEqual(len(review.staged), 1)
            self.assertEqual(review.status, "awaiting_review")

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
    def test_fixable_selection_preserves_scanner_execution_order(self) -> None:
        profile = {
            "solution_profile": {
                "action": "implement_or_fix_adapter",
                "owned": True,
                "profile_status": "reviewed",
                "journey_stage": "validate",
            }
        }
        rows = [
            {
                "id": "gap_shared000001",
                "domain": "vm",
                "step_name": "launch",
                "validation_class": "FirstCheck",
                "status": "not_implemented",
                "remediation": {"auto_fixable": True, "target": "scripts/vm/shared.py"},
                "enrichment": profile,
            },
            {
                "id": "gap_shared000002",
                "domain": "vm",
                "step_name": "launch",
                "validation_class": "SecondCheck",
                "status": "not_implemented",
                "remediation": {"auto_fixable": True, "target": "scripts/vm/shared.py"},
                "enrichment": profile,
            },
            {
                "id": "gap_single000001",
                "domain": "vm",
                "step_name": "describe",
                "validation_class": "OnlyCheck",
                "status": "not_implemented",
                "remediation": {"auto_fixable": True, "target": "scripts/vm/single.py"},
                "enrichment": profile,
            },
        ]

        selected = _select_fixable({"rows": rows}, "vm")

        self.assertEqual(
            [row["id"] for row in selected],
            ["gap_shared000001", "gap_shared000002", "gap_single000001"],
        )

    def test_generator_infrastructure_failure_stops_without_identical_retries(self) -> None:
        calls = {"n": 0}

        def timed_out(command, cwd, request, environment, timeout):
            del cwd, request, environment, timeout
            calls["n"] += 1
            return subprocess.CompletedProcess(command, 124, "", "model timed out")

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, provider = _project(Path(tempdir))
            work = Path(tempdir) / "work"
            work.mkdir()
            (work / "auto-review.json").write_text('{"status":"no_changes"}\n', encoding="utf-8")
            (work / "auto-review.patch").write_text("stale patch\n", encoding="utf-8")
            with self.assertRaisesRegex(AutoWorkflowError, "infrastructure failed"):
                run_auto(
                    project_path,
                    domain="vm",
                    work_dir=work,
                    generator_command=["fixture-generator"],
                    generator_runner=timed_out,
                )

            self.assertEqual(calls["n"], 1)
            self.assertIn("TODO", (provider / "scripts" / "vm" / "launch_instance.py").read_text())
            self.assertFalse((work / "auto-review.json").exists())
            self.assertFalse((work / "auto-review.patch").exists())

    def test_reviewed_multi_file_apply_rolls_back_and_preserves_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            scratch = root / "scratch"
            provider.mkdir()
            scratch.mkdir()
            original_a = provider / "a.py"
            original_b = provider / "b.py"
            scratch_a = scratch / "a.py"
            scratch_b = scratch / "b.py"
            original_a.write_text("old a\n", encoding="utf-8")
            original_b.write_text("old b\n", encoding="utf-8")
            scratch_a.write_text("new a\n", encoding="utf-8")
            scratch_b.write_text("new b\n", encoding="utf-8")
            original_a.chmod(0o755)
            original_b.chmod(0o640)
            scratch_a.chmod(0o600)
            scratch_b.chmod(0o600)
            changes = [
                ChangedFile(
                    path=name,
                    before_sha256=hashlib.sha256((provider / name).read_bytes()).hexdigest(),
                    after_sha256=hashlib.sha256((scratch / name).read_bytes()).hexdigest(),
                    creates_file=False,
                )
                for name in ("a.py", "b.py")
            ]
            real_replace = os.replace
            replace_calls = {"count": 0}

            def fail_second_replace(source, target):
                replace_calls["count"] += 1
                if replace_calls["count"] == 2:
                    raise OSError("simulated second-file failure")
                return real_replace(source, target)

            with (
                patch("isv_readiness.auto.os.replace", side_effect=fail_second_replace),
                self.assertRaisesRegex(AutoWorkflowError, "rolled back"),
            ):
                _apply_to_provider(provider, scratch, changes, root / "backups")

            self.assertEqual(original_a.read_text(encoding="utf-8"), "old a\n")
            self.assertEqual(original_b.read_text(encoding="utf-8"), "old b\n")
            self.assertEqual(original_a.stat().st_mode & 0o777, 0o755)
            self.assertEqual(original_b.stat().st_mode & 0o777, 0o640)

            _apply_to_provider(provider, scratch, changes, root / "backups")
            self.assertEqual(original_a.read_text(encoding="utf-8"), "new a\n")
            self.assertEqual(original_b.read_text(encoding="utf-8"), "new b\n")
            self.assertEqual(original_a.stat().st_mode & 0o777, 0o755)
            self.assertEqual(original_b.stat().st_mode & 0o777, 0o640)

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
        artifact_ref = digest["latest"]["artifact_refs"][0]
        self.assertIn("/failures/", artifact_ref["path"])
        self.assertEqual(len(artifact_ref["sha256"]), 64)

    def test_static_verification_retry_receives_selected_gap_evidence(self) -> None:
        calls = {"n": 0}
        requests: list[dict] = []

        def incomplete_then_valid(command, cwd, request, environment, timeout):
            calls["n"] += 1
            requests.append(json.loads(request))
            result = _generator_runner(command, cwd, request, environment, timeout)
            if calls["n"] == 1:
                output = json.loads(result.stdout)
                content = 'import json\n\nprint(json.dumps({"success": True, "platform": "fixture"}))\n'
                output["changes"][0]["content"] = content
                output["changes"][0]["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                return subprocess.CompletedProcess(command, 0, json.dumps(output), "")
            return result

        with tempfile.TemporaryDirectory() as tempdir:
            project_path, _provider = _project(Path(tempdir))
            review = run_auto(
                project_path,
                domain="vm",
                work_dir=Path(tempdir) / "work",
                generator_command=["fixture-generator"],
                generator_runner=incomplete_then_valid,
            )

        self.assertEqual(review.status, "awaiting_review")
        feedback = next(
            item
            for item in requests[1]["context_pack"]["items"]
            if item["source_id"] == "previous_attempt_feedback"
        )
        digest = json.loads(feedback["content"])
        self.assertIn("instance_id", " ".join(digest["latest"]["details"]))

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

    def test_park_does_not_claim_an_unstaged_patch_exists(self) -> None:
        rows = _config_rows()

        parked = _park(
            {"rows": rows},
            "vm",
            [],
            {adapter_contract_unit(rows[0]): 1},
            3,
        )

        self.assertTrue(all("re-run auto" in item.reason for item in parked))
        self.assertTrue(all("apply" not in item.reason for item in parked))


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
    raw["execution"]["run_environment"] = "staging"
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
    if "audit_context" in payload:
        return _audit_result(command, payload, status="implemented", target=None)
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


def _audit_result(command, payload: dict, *, status: str, target: str | None):
    capability_id = payload["audit_context"]["approved_capabilities"][0]["capability_id"]
    evidence = (
        [
            {
                "effect": "create provider resource",
                "path": "scripts/vm/launch_instance.py",
                "detail": "The setup path calls the provider lifecycle helper and emits its resource ID.",
            }
        ]
        if status == "implemented"
        else []
    )
    output = {
        "schema_version": "0.1.0",
        "domain": payload["audit_context"]["domain"],
        "audit_context_sha256": payload["audit_context_sha256"],
        "auditor": {"adapter": "fixture", "model": "fixture-model"},
        "summary": "Audit the complete approved VM lifecycle",
        "capabilities": [
            {
                "capability_id": capability_id,
                "status": status,
                "step_name": "launch_instance",
                "target": target,
                "expected_effects": {
                    "setup": ["create provider resource"],
                    "test": ["list provider resources"],
                    "teardown": ["delete resource owned by the run"],
                },
                "implementation_evidence": evidence,
                "reason": (
                    "The lifecycle implementation performs the reviewed effects."
                    if status == "implemented"
                    else "The existing setup only inventories a preexisting resource."
                ),
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
