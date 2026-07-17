from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import yaml

from isv_readiness.project import load_project
from isv_readiness.publish import PublishError, _platform_for_domain, publish_project
from isv_readiness.runs import LOG_FILENAME, new_run_dir, write_run_record
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from tests.test_live import COMMIT, _project


class PublishTests(unittest.TestCase):
    def test_platform_mapping_uses_authoritative_domain_names(self) -> None:
        self.assertEqual(_platform_for_domain("network"), "NETWORK")
        self.assertEqual(_platform_for_domain("control_plane"), "CONTROL_PLANE")
        with self.assertRaisesRegex(PublishError, "Cannot infer"):
            _platform_for_domain("unknown")

    def test_unready_project_is_rejected_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=True)
            with patch("isv_readiness.publish._get_jwt_token") as get_token:
                with self.assertRaisesRegex(PublishError, "gaps.json not found"):
                    publish_project(manifest, lab_id=3, commit_resolver=lambda root: COMMIT)
            get_token.assert_not_called()

    def test_malformed_recorded_junit_is_rejected_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            manifest = _ready_project(Path(tempdir), junit_xml="<testsuite>")
            with patch("isv_readiness.publish._get_jwt_token") as get_token:
                with self.assertRaisesRegex(PublishError, "invalid JUnit XML"):
                    publish_project(manifest, lab_id=3, commit_resolver=lambda root: COMMIT)
            get_token.assert_not_called()

    def test_validation_checkout_drift_is_rejected_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            manifest = _ready_project(Path(tempdir))
            with patch("isv_readiness.publish._get_jwt_token") as get_token:
                with self.assertRaisesRegex(PublishError, "drifted"):
                    publish_project(manifest, lab_id=3, commit_resolver=lambda root: "d" * 40)
            get_token.assert_not_called()

    def test_latest_recorded_run_is_published_with_junit_and_exact_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            manifest = _ready_project(Path(tempdir))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "ISV_SERVICE_ENDPOINT": "https://labs.invalid/",
                        "ISV_SSA_ISSUER": "https://ssa.invalid/",
                        "ISV_CLIENT_ID": "client",
                        "ISV_CLIENT_SECRET": "secret",
                    },
                    clear=True,
                ),
                patch("isv_readiness.publish._get_jwt_token", return_value="jwt") as get_token,
                patch(
                    "isv_readiness.publish._post",
                    side_effect=[{"data": {"testRunId": 42}}, {}, {}],
                ) as post,
                redirect_stdout(io.StringIO()),
            ):
                urls = publish_project(
                    manifest,
                    lab_id=3,
                    tags=["acme", "vm"],
                    commit_resolver=lambda root: COMMIT,
                )

            self.assertEqual(urls, ("https://labs.invalid/v1/labs/3/test-runs/42",))
            get_token.assert_called_once_with("https://ssa.invalid", "client", "secret")
            create_payload = post.call_args_list[0].args[1]
            self.assertEqual(create_payload["testTargetType"], "VM")
            self.assertEqual(create_payload["tags"], ["acme", "vm"])
            self.assertIn("gapctl test vm", create_payload["ciReference"])
            self.assertEqual(
                post.call_args_list[1],
                call(
                    "https://labs.invalid/v1/labs/3/test-runs/42/test-results",
                    {"junitXml": '<testsuite tests="1"><testcase name="test_vm[InstanceCreatedCheck]" /></testsuite>'},
                    token="jwt",
                ),
            )

    def test_multi_domain_project_creates_one_typed_portal_run_per_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            manifest = _ready_project(Path(tempdir), domains=("vm", "network"))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "ISV_SERVICE_ENDPOINT": "https://labs.invalid",
                        "ISV_SSA_ISSUER": "https://ssa.invalid",
                        "ISV_CLIENT_ID": "client",
                        "ISV_CLIENT_SECRET": "secret",
                    },
                    clear=True,
                ),
                patch("isv_readiness.publish._get_jwt_token", return_value="jwt"),
                patch(
                    "isv_readiness.publish._post",
                    side_effect=[{"data": {"testRunId": 41}}, {}, {}, {"data": {"testRunId": 42}}, {}, {}],
                ) as post,
                redirect_stdout(io.StringIO()),
            ):
                urls = publish_project(manifest, lab_id=3, commit_resolver=lambda root: COMMIT)

            self.assertEqual(
                urls,
                (
                    "https://labs.invalid/v1/labs/3/test-runs/41",
                    "https://labs.invalid/v1/labs/3/test-runs/42",
                ),
            )
            self.assertEqual(post.call_args_list[0].args[1]["testTargetType"], "VM")
            self.assertEqual(post.call_args_list[3].args[1]["testTargetType"], "NETWORK")


def _ready_project(
    root: Path,
    *,
    domains: tuple[str, ...] = ("vm",),
    junit_xml: str | None = None,
) -> Path:
    project, manifest = _project(root, allow_live=True)
    if domains != ("vm",):
        raw_project = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        raw_project["assessment"]["domains"] = list(domains)
        manifest.write_text(yaml.safe_dump(raw_project, sort_keys=False), encoding="utf-8")

        profile_path = manifest.parent / "solution-profile.yaml"
        raw_profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        base_scope = raw_profile["domains"][0]
        raw_profile["domains"] = []
        for domain in domains:
            scope = copy.deepcopy(base_scope)
            scope["domain"] = domain
            scope["name"] = domain.replace("_", " ").title()
            scope["capabilities"] = []
            raw_profile["domains"].append(scope)
        profile_path.write_text(yaml.safe_dump(raw_profile, sort_keys=False), encoding="utf-8")
        project = load_project(manifest)

    report = scan_provider(
        ScanOptions(
            provider_repo=project.provider_root(manifest),
            domains=list(domains),
            validation_root=project.validation_root(manifest),
        )
    ).to_dict()
    for row in report["rows"]:
        row["status"] = "pass"
    for domain in domains:
        source = next(row for row in report["rows"] if row["domain"] == domain)
        dynamic = copy.deepcopy(source)
        dynamic.update(id=f"gap_dynamic_{domain}", detection="dynamic", status="pass")
        report["rows"].append(dynamic)
    (manifest.parent / "gaps.json").write_text(json.dumps(report), encoding="utf-8")

    for domain in domains:
        run_id, run_dir = new_run_dir(
            manifest.parent / ".gapctl" / "runs",
            domain,
            created_at="20260717T120000Z",
        )
        (run_dir / "junit.xml").write_text(
            junit_xml
            or f'<testsuite tests="1"><testcase name="test_{domain}[InstanceCreatedCheck]" /></testsuite>',
            encoding="utf-8",
        )
        (run_dir / LOG_FILENAME).write_text("PASS\n", encoding="utf-8")
        write_run_record(
            run_dir,
            run_id=run_id,
            domain=domain,
            config=f"isvctl/configs/providers/acme/config/{domain}.yaml",
            exit_code=0,
        )
    self_check = load_project(manifest)
    assert self_check.validation.resolved_commit == COMMIT
    return manifest


if __name__ == "__main__":
    unittest.main()
