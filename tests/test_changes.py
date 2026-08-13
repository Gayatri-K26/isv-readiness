from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.changes import (
    Change,
    ChangeSet,
    build_change_proposal,
    canonical_sha256,
    change_set_from_dict,
)
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.schema import load_schema

ROOT = Path(__file__).resolve().parents[1]


class ChangeProposalTests(unittest.TestCase):
    def test_guarded_multi_file_proposal_allows_selected_script_and_domain_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "launch.py"
            config = provider / "config" / "vm.yaml"
            script.parent.mkdir(parents=True)
            config.parent.mkdir()
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            config.write_text(
                "# keep this provider note\n"
                "commands:\n"
                "  vm:\n"
                "    steps:\n"
                "      - name: launch\n"
                "        command: python ../scripts/vm/launch.py\n"
                "        timeout: 60\n",
                encoding="utf-8",
            )
            updated_config = config.read_text(encoding="utf-8").replace("timeout: 60", "timeout: 1200")
            changes = _change_set(
                [
                    ("provider", "scripts/vm/launch.py", "replace", "print({'success': True})\n"),
                    ("provider", "config/vm.yaml", "replace", updated_config),
                ]
            )

            proposal = build_change_proposal(_report(), provider_repo=provider, change_set=changes)

            self.assertEqual(len(proposal.files), 2)
            self.assertIn("a/acme/scripts/vm/launch.py", proposal.patch)
            self.assertIn("a/acme/config/vm.yaml", proposal.patch)
            self.assertEqual(script.read_text(encoding="utf-8"), "raise NotImplementedError()\n")
            schema = load_schema("change-proposal.schema.json")
            jsonschema.validate(proposal.to_dict(), schema)

    def test_domain_config_change_may_only_touch_the_selected_step_block(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            config = provider / "config" / "vm.yaml"
            config.parent.mkdir(parents=True)
            original = (
                "# provider comments and formatting must survive\n"
                "commands:\n"
                "  vm:\n"
                "    steps:\n"
                "      - name: launch\n"
                "        command: python ../scripts/vm/launch.py\n"
                "        args:\n"
                "          - --region\n"
                "          - '{{region}}'\n"
                "        timeout: 60\n"
                "\n"
                "      - name: describe\n"
                "        command: python ../scripts/vm/describe.py\n"
                "        timeout: 60\n"
            )
            config.write_text(original, encoding="utf-8")
            selected_step = (
                "      - name: query_health\n"
                "        command: python ../scripts/vm/query_health.py\n"
                "        timeout: 60\n"
                "\n"
            )
            candidate = original.replace("      - name: describe\n", selected_step + "      - name: describe\n")
            report = _report(target="config/vm.yaml", step_name="query_health")

            proposal = build_change_proposal(
                report,
                provider_repo=provider,
                change_set=_change_set([("provider", "config/vm.yaml", "replace", candidate)]),
            )
            self.assertEqual(len(proposal.files), 1)

            reformatted = candidate.replace(
                "        args:\n          - --region\n          - '{{region}}'\n",
                "        args: [--region, '{{region}}']\n",
            )
            with self.assertRaisesRegex(FixGuardrailError, "outside that step block"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set([("provider", "config/vm.yaml", "replace", reformatted)]),
                )

            unrelated = candidate.replace(
                "      - name: describe\n        command: python ../scripts/vm/describe.py\n        timeout: 60\n",
                "      - name: describe\n        command: python ../scripts/vm/describe.py\n        timeout: 120\n",
            )
            with self.assertRaisesRegex(FixGuardrailError, "outside that step block"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set([("provider", "config/vm.yaml", "replace", unrelated)]),
                )

    def test_rejects_unrelated_config_missing_primary_target_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "scripts" / "vm" / "launch.py").write_text("pass\n", encoding="utf-8")
            (provider / "config").mkdir()
            (provider / "config" / "network.yaml").write_text("tests: {}\n", encoding="utf-8")

            with self.assertRaisesRegex(FixGuardrailError, "selected domain file"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [
                            ("provider", "scripts/vm/launch.py", "replace", "print('safe')\n"),
                            ("provider", "config/network.yaml", "replace", "tests:\n  changed: true\n"),
                        ]
                    ),
                )
            with self.assertRaisesRegex(FixGuardrailError, "does not include the selected gap target"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/helper.py", "create", "print('safe')\n")]
                    ),
                )
            with self.assertRaisesRegex(FixGuardrailError, "secret-looking"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", 'API_KEY = "secret-value-123"\n')]
                    ),
                )
            with self.assertRaisesRegex(FixGuardrailError, "scripts/common"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [
                            ("provider", "scripts/vm/launch.py", "replace", "print('safe')\n"),
                            ("provider", "scripts/client.py", "create", "print('shared')\n"),
                        ]
                    ),
                )

    def test_kubernetes_wrapper_is_the_only_allowed_providers_root_change(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            providers = Path(tempdir) / "providers"
            provider = providers / "acme"
            provider.mkdir(parents=True)
            wrapper = providers / "acme.yaml"
            wrapper.write_text("import: ../suites/k8s.yaml\n", encoding="utf-8")
            report = _report(domain="kubernetes", target="acme.yaml")
            change_set = _change_set(
                [("providers", "acme.yaml", "replace", "import: ../suites/k8s.yaml\nversion: '1.0'\n")]
            )

            proposal = build_change_proposal(report, provider_repo=provider, change_set=change_set)
            self.assertEqual(proposal.files[0].target_root, "providers")

            with self.assertRaisesRegex(FixGuardrailError, "selected provider wrapper"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("providers", "other.yaml", "replace", "version: '1.0'\n")]
                    ),
                )

    def test_change_set_hashes_and_duplicate_paths_are_enforced(self) -> None:
        raw = _change_set([("provider", "scripts/vm/launch.py", "replace", "pass\n")]).to_dict()
        raw["changes"][0]["content_sha256"] = "0" * 64
        with self.assertRaisesRegex(FixGuardrailError, "content hash"):
            change_set_from_dict(raw)

        raw = _change_set([("provider", "scripts/vm/launch.py", "replace", "pass\n")]).to_dict()
        raw["changes"].append(dict(raw["changes"][0]))
        with self.assertRaisesRegex(FixGuardrailError, "duplicate"):
            change_set_from_dict(raw)

    def test_draft_profile_cannot_authorize_a_change(self) -> None:
        report = _report()
        report["rows"][0]["enrichment"]["solution_profile"]["profile_status"] = "draft"
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "launch.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            with self.assertRaisesRegex(FixGuardrailError, "not eligible for generation"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", "print('safe')\n")]
                    ),
                )

    def test_provider_neutral_runtime_and_evidence_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "launch.py"
            script.parent.mkdir(parents=True)
            script.write_text("import os\nraise NotImplementedError()\n", encoding="utf-8")

            undeclared = (
                "import os\n\n"
                "def required_env(name):\n"
                "    return os.environ.get(name)\n\n"
                "print(required_env('NEW_PROVIDER_INPUT'))\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "NEW_PROVIDER_INPUT"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", undeclared)]
                    ),
                    allowed_environment=["ACME_TOKEN"],
                )

            declared = undeclared.replace("NEW_PROVIDER_INPUT", "ACME_TOKEN")
            proposal = build_change_proposal(
                _report(),
                provider_repo=provider,
                change_set=_change_set(
                    [("provider", "scripts/vm/launch.py", "replace", declared)]
                ),
                allowed_environment=["ACME_TOKEN"],
            )
            self.assertEqual(len(proposal.files), 1)

            response_reader = "payload = get_response()\nprint({'success': bool(payload['stdout'])})\n"
            proposal = build_change_proposal(
                _report(),
                provider_repo=provider,
                change_set=_change_set(
                    [("provider", "scripts/vm/launch.py", "replace", response_reader)]
                ),
            )
            self.assertEqual(len(proposal.files), 1)

            insecure = "import ssl\ncontext = ssl._create_unverified_context()\n"
            with self.assertRaisesRegex(FixGuardrailError, "insecure TLS"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", insecure)]
                    ),
                )

            insecure_ssh = (
                "import subprocess\n"
                "subprocess.run(['ssh', '-o', 'StrictHostKeyChecking=no', 'host', 'true'])\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "insecure SSH"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", insecure_ssh)]
                    ),
                )

            raw_output = "result = {'success': True}\nresult['output_snippet'] = 'raw console'\n"
            with self.assertRaisesRegex(FixGuardrailError, "raw provider output"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", raw_output)]
                    ),
                )

    def test_authenticated_transport_requires_strict_https_base_url_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "common" / "client.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            report = _report(target="scripts/common/client.py")
            unsafe = (
                "import os\n"
                "from urllib.request import Request, urlopen\n"
                "server = os.environ['ACME_SERVER']\n"
                "request = Request(server + '/v1/vms', headers={'Authorization': 'Bearer token'})\n"
                "urlopen(request)\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "strict endpoint guard"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/common/client.py", "replace", unsafe)]
                    ),
                    allowed_environment=["ACME_SERVER"],
                )

            safe = (
                "import os\n"
                "from urllib.parse import urlsplit\n"
                "from urllib.request import Request, urlopen\n"
                "server = os.environ['ACME_SERVER']\n"
                "parsed = urlsplit(server)\n"
                "if (parsed.scheme != 'https' or not parsed.hostname or parsed.username "
                "or parsed.password or parsed.query or parsed.fragment):\n"
                "    raise ValueError('invalid endpoint')\n"
                "request = Request(server + '/v1/vms', headers={'Authorization': 'Bearer token'})\n"
                "urlopen(request)\n"
            )
            proposal = build_change_proposal(
                report,
                provider_repo=provider,
                change_set=_change_set(
                    [("provider", "scripts/common/client.py", "replace", safe)]
                ),
                allowed_environment=["ACME_SERVER"],
            )
            self.assertEqual(len(proposal.files), 1)

            lifecycle = provider / "scripts" / "vm" / "launch.py"
            lifecycle.parent.mkdir(parents=True, exist_ok=True)
            lifecycle.write_text("raise NotImplementedError()\n", encoding="utf-8")
            with self.assertRaisesRegex(FixGuardrailError, "provider-shared client"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", safe)]
                    ),
                    allowed_environment=["ACME_SERVER"],
                )

            private_helper = provider / "scripts" / "vm" / "_transport_impl.py"
            private_helper.write_text("raise NotImplementedError()\n", encoding="utf-8")
            private_proposal = build_change_proposal(
                _report(target="scripts/vm/_transport_impl.py"),
                provider_repo=provider,
                change_set=_change_set(
                    [("provider", "scripts/vm/_transport_impl.py", "replace", safe)]
                ),
                allowed_environment=["ACME_SERVER"],
            )
            self.assertEqual(len(private_proposal.files), 1)

    def test_cleanup_requires_absent_success_and_independent_error_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "delete_resources.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            report = _report(target="scripts/vm/delete_resources.py", step_name="teardown")

            no_absence = "client.delete('vm-1')\n"
            with self.assertRaisesRegex(FixGuardrailError, "already-absent success path"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/delete_resources.py", "replace", no_absence)]
                    ),
                )

            fail_fast = (
                "cleanup_errors = []\n"
                "try:\n"
                "    client.delete('key-1')\n"
                "    client.delete('account-1')\n"
                "except HttpError as exc:\n"
                "    if exc.status_code != 404:\n"
                "        cleanup_errors.append('cleanup failed')\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "fail-fast try block"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/delete_resources.py", "replace", fail_fast)]
                    ),
                )

            safe = (
                "cleanup_errors = []\n"
                "for resource in ('key-1', 'account-1'):\n"
                "    try:\n"
                "        client.delete(resource)\n"
                "    except HttpError as exc:\n"
                "        if exc.status_code != 404:\n"
                "            cleanup_errors.append('cleanup failed')\n"
            )
            proposal = build_change_proposal(
                report,
                provider_repo=provider,
                change_set=_change_set(
                    [("provider", "scripts/vm/delete_resources.py", "replace", safe)]
                ),
            )
            self.assertEqual(len(proposal.files), 1)

    def test_cleanup_guard_recognizes_descriptive_delete_method_names(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "delete_resources.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            report = _report(target="scripts/vm/delete_resources.py", step_name="teardown")

            with self.assertRaisesRegex(FixGuardrailError, "already-absent success path"):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [
                            (
                                "provider",
                                "scripts/vm/delete_resources.py",
                                "replace",
                                "client.delete_project('project-1')\n",
                            )
                        ]
                    ),
                )

    def test_rejects_dynamic_code_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "probe.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")

            for call in ("exec(payload)", "eval(payload)", "compile(payload, 'probe.py', 'exec')"):
                candidate = f"payload = 'print(1)'\n{call}\n"
                with self.subTest(call=call), self.assertRaisesRegex(
                    FixGuardrailError, "literal source code directly in change.content"
                ):
                    build_change_proposal(
                        _report(target="scripts/vm/probe.py"),
                        provider_repo=provider,
                        change_set=_change_set(
                            [("provider", "scripts/vm/probe.py", "replace", candidate)]
                        ),
                    )

    def test_rejects_direct_authenticated_shell_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "slurm" / "setup.sh"
            script.parent.mkdir(parents=True)
            script.write_text("exit 1\n", encoding="utf-8")
            candidate = (
                "#!/bin/bash\n"
                "curl -H \"Authorization: Bearer $ACME_TOKEN\" \"$ACME_SERVER/v1/clusters\"\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "directly in a shell script"):
                build_change_proposal(
                    _report(target="scripts/slurm/setup.sh"),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/slurm/setup.sh", "replace", candidate)]
                    ),
                    allowed_environment=["ACME_TOKEN", "ACME_SERVER"],
                )

    def test_rejects_internal_deadline_beyond_configured_step_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "vm" / "launch.py"
            config = provider / "config" / "vm.yaml"
            script.parent.mkdir(parents=True)
            config.parent.mkdir()
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            config.write_text(
                "commands:\n"
                "  vm:\n"
                "    steps:\n"
                "      - name: launch\n"
                "        command: python ../scripts/vm/launch.py\n"
                "        timeout: 60\n",
                encoding="utf-8",
            )
            candidate = (
                "import time\n"
                "deadline = time.monotonic() + 900\n"
                "print({'success': True, 'deadline': deadline})\n"
            )
            with self.assertRaisesRegex(FixGuardrailError, "900s exceeds configured timeout 60s"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", candidate)]
                    ),
                )

            raised_timeout = config.read_text(encoding="utf-8").replace(
                "timeout: 60",
                "timeout: 1200",
            )
            proposal = build_change_proposal(
                _report(),
                provider_repo=provider,
                change_set=_change_set(
                    [
                        ("provider", "scripts/vm/launch.py", "replace", candidate),
                        ("provider", "config/vm.yaml", "replace", raised_timeout),
                    ]
                ),
            )
            self.assertEqual(len(proposal.files), 2)

    def test_rejects_lifecycle_timeouts_below_the_authoritative_source_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "acme"
            script = provider / "scripts" / "bare_metal" / "reboot_instance.py"
            config = provider / "config" / "bare_metal.yaml"
            script.parent.mkdir(parents=True)
            config.parent.mkdir()
            script.write_text("raise NotImplementedError()\n", encoding="utf-8")
            original_config = (
                "commands:\n"
                "  bare_metal:\n"
                "    steps:\n"
                "      - name: reboot_instance\n"
                "        command: python ../scripts/bare_metal/reboot_instance.py\n"
                "        timeout: 60\n"
            )
            config.write_text(original_config, encoding="utf-8")
            report = _report(
                domain="bare_metal",
                target="scripts/bare_metal/reboot_instance.py",
                step_name="reboot_instance",
            )
            constraints = {"lifecycle_step_timeout_seconds": 1200.0}
            short_deadline = (
                "import time\n"
                "OPERATION_DEADLINE_SECONDS = 50\n"
                "deadline = time.monotonic() + OPERATION_DEADLINE_SECONDS\n"
            )

            with self.assertRaisesRegex(
                FixGuardrailError,
                "configured timeout 60s is below the source-backed lifecycle minimum 1200s",
            ):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/bare_metal/reboot_instance.py", "replace", short_deadline)]
                    ),
                    contract_constraints=constraints,
                )

            raised_config = original_config.replace("timeout: 60", "timeout: 1260")
            with self.assertRaisesRegex(
                FixGuardrailError,
                "internal deadline 50s is below the source-backed lifecycle minimum 1200s",
            ):
                build_change_proposal(
                    report,
                    provider_repo=provider,
                    change_set=_change_set(
                        [
                            ("provider", "scripts/bare_metal/reboot_instance.py", "replace", short_deadline),
                            ("provider", "config/bare_metal.yaml", "replace", raised_config),
                        ]
                    ),
                    contract_constraints=constraints,
                )

            compliant_deadline = short_deadline.replace("= 50", "= 1200")
            proposal = build_change_proposal(
                report,
                provider_repo=provider,
                change_set=_change_set(
                    [
                        ("provider", "scripts/bare_metal/reboot_instance.py", "replace", compliant_deadline),
                        ("provider", "config/bare_metal.yaml", "replace", raised_config),
                    ]
                ),
                contract_constraints=constraints,
            )
            self.assertEqual(len(proposal.files), 2)


def _change_set(values: list[tuple[str, str, str, str]]) -> ChangeSet:
    changes = tuple(
        Change(
            target_root=root,
            path=path,
            operation=operation,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            rationale="Address selected validation gap",
        )
        for root, path, operation, content in values
    )
    return ChangeSet(
        schema_version="0.1.0",
        gap_id="gap_0123456789ab",
        context_pack_sha256=canonical_sha256({"gap": "fixture"}),
        generator={"adapter": "fixture", "model": None},
        summary="Fixture change",
        changes=changes,
    )


def _report(
    *,
    domain: str = "vm",
    target: str = "scripts/vm/launch.py",
    step_name: str = "launch",
) -> dict:
    return {
        "rows": [
            {
                "id": "gap_0123456789ab",
                "domain": domain,
                "step_name": step_name,
                "status": "not_implemented",
                "detection": "static",
                "remediation": {"auto_fixable": True, "target": target},
                "enrichment": {
                    "solution_profile": {
                        "action": "implement_or_fix_adapter",
                        "owned": True,
                        "profile_status": "reviewed",
                        "journey_stage": "validate",
                    }
                },
            }
        ]
    }


if __name__ == "__main__":
    unittest.main()
