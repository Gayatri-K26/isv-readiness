from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from isv_readiness.publish import PublishError, _detect_platform, publish_bundle


class PublishTests(unittest.TestCase):
    def test_platform_detection_uses_authoritative_domain_names(self) -> None:
        self.assertEqual(_detect_platform(["network"]), "NETWORK")
        self.assertEqual(_detect_platform(["control_plane"]), "CONTROL_PLANE")
        with self.assertRaisesRegex(PublishError, "multiple platform types"):
            _detect_platform(["vm", "network"])

    def test_incomplete_bundle_is_rejected_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = _bundle(Path(tempdir), outcome="incomplete")
            with patch("isv_readiness.publish._get_jwt_token") as get_token:
                with self.assertRaisesRegex(PublishError, "incomplete evidence bundle"):
                    publish_bundle(bundle, lab_id=3)
            get_token.assert_not_called()

    def test_missing_junit_is_rejected_before_remote_test_run_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = _bundle(Path(tempdir))
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
                patch("isv_readiness.publish._get_jwt_token") as get_token,
            ):
                with self.assertRaisesRegex(PublishError, "JUnit XML not found"):
                    publish_bundle(bundle, lab_id=3, junit_xml_path=bundle / "missing.xml")
            get_token.assert_not_called()

    def test_malformed_junit_is_rejected_before_remote_test_run_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = _bundle(Path(tempdir))
            junit = bundle / "junit.xml"
            junit.write_text("<testsuite>", encoding="utf-8")
            with patch("isv_readiness.publish._get_jwt_token") as get_token:
                with self.assertRaisesRegex(PublishError, "not well formed"):
                    publish_bundle(bundle, lab_id=3, junit_xml_path=junit)
            get_token.assert_not_called()

    def test_completed_bundle_is_published_with_exact_platform_and_deduplicated_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = _bundle(Path(tempdir), domains=["network"])
            junit = bundle / "junit.xml"
            junit.write_text('<testsuite tests="1" />', encoding="utf-8")
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
            ):
                url = publish_bundle(bundle, lab_id=3, junit_xml_path=junit, tags=["acme", "network"])

            self.assertEqual(url, "https://labs.invalid/v1/labs/3/test-runs/42")
            get_token.assert_called_once_with("https://ssa.invalid", "client", "secret")
            create_payload = post.call_args_list[0].args[1]
            self.assertEqual(create_payload["testTargetType"], "NETWORK")
            self.assertEqual(create_payload["tags"], ["acme", "network"])
            self.assertEqual(
                post.call_args_list[1],
                call(
                    "https://labs.invalid/v1/labs/3/test-runs/42/test-results",
                    {"junitXml": '<testsuite tests="1" />'},
                    token="jwt",
                ),
            )


def _bundle(root: Path, *, outcome: str = "validation_complete", domains: list[str] | None = None) -> Path:
    bundle = root / "bundle"
    bundle.mkdir()
    domain_names = domains or ["vm"]
    manifest = {
        "schema_version": "0.1.0",
        "provider": "acme",
        "outcome": outcome,
        "validation": {
            "url": "https://github.com/NVIDIA/ai-cloud-validation.git",
            "ref": "main",
            "pinned_commit": "a" * 40,
            "current_commit": "a" * 40,
        },
        "domains": [
            {"domain": domain, "status": "complete" if outcome == "validation_complete" else "blocked", "reason": "test"}
            for domain in domain_names
        ],
        "provider_files": [],
        "files": [],
        "excluded_sensitive": [],
    }
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


if __name__ == "__main__":
    unittest.main()
