from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from isv_readiness.generators import (
    DEFAULT_GENERATOR_MAX_REQUEST_BYTES,
    FileExchangeRunner,
    GeneratorConfigurationError,
    GeneratorExchangeError,
    GeneratorRequestExported,
    load_generator_registry,
    resolve_generator_spec,
)


class GeneratorRegistryTests(unittest.TestCase):
    def test_registered_adapter_controls_command_environment_and_bounded_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = Path(tempdir) / "generators.yaml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "generators": {
                            "internal-agent": {
                                "protocol_version": "0.1.0",
                                "command": ["/opt/internal/adapter", "--profile", "isv"],
                                "pass_env": ["INTERNAL_AGENT_TOKEN"],
                                "timeout_seconds": 7200,
                                "idle_timeout_seconds": 900,
                                "max_request_bytes": 8_000_000,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            spec = resolve_generator_spec(
                "internal-agent",
                executable_dir=Path("/unused"),
                config_path=config,
            )

        self.assertEqual(spec.command, ("/opt/internal/adapter", "--profile", "isv"))
        self.assertEqual(spec.pass_env, ("INTERNAL_AGENT_TOKEN",))
        self.assertEqual(spec.timeout_seconds, 7200)
        self.assertEqual(spec.idle_timeout_seconds, 900)
        self.assertEqual(spec.max_request_bytes, 8_000_000)

    def test_registry_overrides_builtin_alias_and_unknown_name_is_an_adapter_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = root / "generators.yaml"
            config.write_text(
                "generators:\n  codex:\n    command: [/custom/codex-adapter]\n",
                encoding="utf-8",
            )
            registered = resolve_generator_spec("codex", executable_dir=root, config_path=config)
            unknown = resolve_generator_spec(
                "gemini-adapter",
                executable_dir=root,
                environment={"HOME": str(root)},
            )

        self.assertEqual(registered.command, ("/custom/codex-adapter",))
        self.assertEqual(unknown.command, ("gemini-adapter",))
        self.assertEqual(unknown.max_request_bytes, DEFAULT_GENERATOR_MAX_REQUEST_BYTES)

    def test_invalid_protocol_secret_assignment_and_unbounded_timeout_are_rejected(self) -> None:
        cases = (
            {"protocol_version": "9.0", "command": ["agent"]},
            {"command": ["agent"], "pass_env": ["TOKEN=secret"]},
            {"command": ["agent"], "timeout_seconds": 99_999},
        )
        for entry in cases:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tempdir:
                config = Path(tempdir) / "generators.yaml"
                config.write_text(
                    yaml.safe_dump({"generators": {"bad": entry}}),
                    encoding="utf-8",
                )
                with self.assertRaises(GeneratorConfigurationError):
                    load_generator_registry(config_path=config)

    def test_explicit_missing_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            missing = Path(tempdir) / "missing.yaml"
            with self.assertRaisesRegex(GeneratorConfigurationError, "not found"):
                load_generator_registry(config_path=missing)


class FileExchangeTests(unittest.TestCase):
    def test_exports_complete_request_then_imports_exact_response_and_computes_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            request_path = root / "request.json"
            response_path = root / "response.json"
            runner = FileExchangeRunner(request_path)
            request = {
                "schema_version": "0.1.0",
                "context_pack_sha256": "a" * 64,
                "context_pack": {"complete_reference": "END-OF-SOURCE"},
            }
            serialized = json.dumps(request, sort_keys=True)

            with self.assertRaises(GeneratorRequestExported):
                runner(["exchange"], root, serialized, {}, 1800)

            self.assertEqual(json.loads(request_path.read_text(encoding="utf-8")), request)
            content = "print('provider adapter')\n"
            response = {
                "changes": [
                    {
                        "content": content,
                        "content_sha256": "0" * 64,
                    }
                ]
            }
            response_path.write_text(json.dumps(response), encoding="utf-8")
            importer = FileExchangeRunner(request_path, response_path)
            completed = importer(["exchange"], root, serialized, {}, 1800)
            imported = json.loads(completed.stdout)

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                imported["changes"][0]["content_sha256"],
                hashlib.sha256(content.encode()).hexdigest(),
            )
            next_request = json.dumps({**request, "context_pack_sha256": "b" * 64}, sort_keys=True)
            with self.assertRaises(GeneratorRequestExported):
                importer(["exchange"], root, next_request, {}, 1800)
            self.assertEqual(
                json.loads(request_path.read_text(encoding="utf-8"))["context_pack_sha256"],
                "b" * 64,
            )

    def test_import_requires_the_exact_previously_exported_request(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text('{"request": "old"}\n', encoding="utf-8")
            response_path.write_text("{}\n", encoding="utf-8")
            runner = FileExchangeRunner(request_path, response_path)

            with self.assertRaisesRegex(GeneratorExchangeError, "differs"):
                runner(["exchange"], root, '{"request": "new"}', {}, 1800)

    def test_import_requires_a_prior_export(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            response = root / "response.json"
            response.write_text("{}\n", encoding="utf-8")
            runner = FileExchangeRunner(root / "missing-request.json", response)
            with self.assertRaisesRegex(GeneratorExchangeError, "run with --generator export"):
                runner(["exchange"], root, "{}", {}, 1800)


if __name__ == "__main__":
    unittest.main()
