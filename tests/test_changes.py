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
            config.write_text("tests:\n  description: old\n", encoding="utf-8")
            changes = _change_set(
                [
                    ("provider", "scripts/vm/launch.py", "replace", "print({'success': True})\n"),
                    ("provider", "config/vm.yaml", "replace", "tests:\n  description: updated\n"),
                ]
            )

            proposal = build_change_proposal(_report(), provider_repo=provider, change_set=changes)

            self.assertEqual(len(proposal.files), 2)
            self.assertIn("a/acme/scripts/vm/launch.py", proposal.patch)
            self.assertIn("a/acme/config/vm.yaml", proposal.patch)
            self.assertEqual(script.read_text(encoding="utf-8"), "raise NotImplementedError()\n")
            schema = load_schema("change-proposal.schema.json")
            jsonschema.validate(proposal.to_dict(), schema)

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

            raw_output = "result = {'success': True}\nresult['output_snippet'] = 'raw console'\n"
            with self.assertRaisesRegex(FixGuardrailError, "raw provider output"):
                build_change_proposal(
                    _report(),
                    provider_repo=provider,
                    change_set=_change_set(
                        [("provider", "scripts/vm/launch.py", "replace", raw_output)]
                    ),
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


def _report(*, domain: str = "vm", target: str = "scripts/vm/launch.py") -> dict:
    return {
        "rows": [
            {
                "id": "gap_0123456789ab",
                "domain": domain,
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
