from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.agent import run_agent_turn
from isv_readiness.bundle import BundleError, build_bundle
from tests.test_agent import COMMIT, _generator_runner, _live_runner, _project

ROOT = Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    def test_completed_validation_bundle_includes_evidence_but_not_sensitive_context(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            project_path, _ = _project(root)
            work = root / "work"
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
                approval_patch_sha256=state.patch_sha256,
                apply_changes=True,
                run_live=True,
                live_runner=_live_runner,
                commit_resolver=lambda root: COMMIT,
                environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "secret"},
            )
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

            output = root / "bundle"
            manifest = build_bundle(
                project_path,
                agent_work_dirs=[work],
                output_dir=output,
                commit_resolver=lambda root: COMMIT,
            )

            self.assertEqual(manifest.outcome, "validation_complete")
            included = {item.path for item in manifest.files}
            self.assertTrue(any("proposal-" in path and path.endswith(".patch") for path in included))
            self.assertTrue(any("verification-" in path for path in included))
            self.assertTrue(any("application-" in path for path in included))
            self.assertFalse(any("context-" in path or "changes-" in path for path in included))
            self.assertFalse(any(path.suffix == ".log" for path in output.rglob("*")))
            schema = json.loads((ROOT / "schemas" / "bundle-manifest.schema.json").read_text(encoding="utf-8"))
            jsonschema.validate(manifest.to_dict(), schema)

            with self.assertRaisesRegex(BundleError, "Refusing to overwrite"):
                build_bundle(
                    project_path,
                    agent_work_dirs=[work],
                    output_dir=output,
                    commit_resolver=lambda root: COMMIT,
                )


if __name__ == "__main__":
    unittest.main()
