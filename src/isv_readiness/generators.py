"""Local generator-adapter discovery and file-exchange fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from isv_readiness.generator_limits import (
    GENERATOR_ADAPTER_TIMEOUT_SECONDS,
    MAX_GENERATOR_REQUEST_BYTES,
    MAX_GENERATOR_TIMEOUT_SECONDS,
)

GENERATOR_PROTOCOL_VERSION = "0.1.0"
DEFAULT_GENERATOR_MAX_REQUEST_BYTES = 5_000_000
GENERATOR_CONFIG_ENV = "GAPCTL_GENERATORS_CONFIG"
GENERATOR_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BUILTIN_EXECUTABLES = {
    "claude": "gapctl-claude-generator",
    "codex": "gapctl-codex-generator",
}
_CONFIG_FIELDS = frozenset(
    {
        "command",
        "idle_timeout_seconds",
        "max_request_bytes",
        "pass_env",
        "protocol_version",
        "timeout_seconds",
    }
)


class GeneratorConfigurationError(ValueError):
    """Raised when local adapter configuration is invalid or ambiguous."""


class GeneratorExchangeError(ValueError):
    """Raised when an exported request and imported response cannot be paired."""


class GeneratorRequestExported(Exception):
    """Signal that generation paused after materializing a complete request."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Generator request exported: {path}")


@dataclass(frozen=True)
class GeneratorSpec:
    name: str
    command: tuple[str, ...]
    pass_env: tuple[str, ...] = ()
    timeout_seconds: int = GENERATOR_ADAPTER_TIMEOUT_SECONDS
    idle_timeout_seconds: int | None = None
    max_request_bytes: int = DEFAULT_GENERATOR_MAX_REQUEST_BYTES


class FileExchangeRunner:
    """Export one exact request or consume one response before exporting again."""

    def __init__(self, request_path: Path, response_path: Path | None = None):
        self.request_path = request_path.expanduser().resolve()
        self.response_path = response_path.expanduser().resolve() if response_path else None
        self._response_consumed = False

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        request: str,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment, timeout_seconds
        raw_request = _json_object(request, "Generator request")
        if self.response_path is not None and not self._response_consumed:
            if not self.request_path.is_file():
                raise GeneratorExchangeError(
                    f"No exported request exists at {self.request_path}; run with --generator export first."
                )
            exported = _json_object(
                self.request_path.read_text(encoding="utf-8"),
                f"Exported request {self.request_path}",
            )
            if exported != raw_request:
                raise GeneratorExchangeError(
                    "The current generator request differs from the exported request. "
                    "Export a new request before importing a response."
                )
            if not self.response_path.is_file():
                raise GeneratorExchangeError(f"Generator response file not found: {self.response_path}")
            response = _json_object(
                self.response_path.read_text(encoding="utf-8"),
                f"Generator response {self.response_path}",
            )
            _fill_content_hashes(response)
            self._response_consumed = True
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        self.request_path.parent.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(
            json.dumps(raw_request, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        raise GeneratorRequestExported(self.request_path)


def resolve_generator_spec(
    name: str,
    *,
    executable_dir: Path,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> GeneratorSpec:
    if not isinstance(name, str) or not name.strip():
        raise GeneratorConfigurationError("Generator name or executable must not be empty.")
    selected = name.strip()
    registry = load_generator_registry(config_path=config_path, environment=environment)
    if selected in registry:
        return registry[selected]
    if selected == "export":
        return GeneratorSpec(name="export", command=("gapctl-file-exchange",))
    builtin = _BUILTIN_EXECUTABLES.get(selected)
    if builtin:
        sibling = executable_dir / builtin
        executable = str(sibling) if sibling.is_file() else builtin
        return GeneratorSpec(name=selected, command=(executable,))
    return GeneratorSpec(name=selected, command=(_expand_executable(selected),))


def load_generator_registry(
    *,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, GeneratorSpec]:
    env = environment if environment is not None else os.environ
    explicit = config_path is not None or bool(env.get(GENERATOR_CONFIG_ENV))
    path = (config_path or default_generator_config_path(env)).expanduser()
    if not path.is_file():
        if explicit:
            raise GeneratorConfigurationError(f"Generator configuration file not found: {path}")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GeneratorConfigurationError(f"Could not load generator configuration {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw).difference({"generators"}):
        raise GeneratorConfigurationError(
            f"Generator configuration {path} must contain only a 'generators' mapping."
        )
    generators = raw.get("generators")
    if not isinstance(generators, dict):
        raise GeneratorConfigurationError(
            f"Generator configuration {path} must contain a 'generators' mapping."
        )
    resolved: dict[str, GeneratorSpec] = {}
    for name, entry in generators.items():
        if not isinstance(name, str) or not GENERATOR_NAME_RE.fullmatch(name) or name == "export":
            raise GeneratorConfigurationError(f"Invalid or reserved generator name in {path}: {name!r}")
        resolved[name] = _parse_generator_entry(name, entry, path)
    return resolved


def default_generator_config_path(environment: Mapping[str, str] | None = None) -> Path:
    env = environment if environment is not None else os.environ
    explicit = env.get(GENERATOR_CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    config_home = env.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "gapctl" / "generators.yaml"
    home = env.get("HOME")
    return (Path(home).expanduser() if home else Path.home()) / ".config" / "gapctl" / "generators.yaml"


def _parse_generator_entry(name: str, entry: Any, path: Path) -> GeneratorSpec:
    if not isinstance(entry, dict):
        raise GeneratorConfigurationError(f"Generator '{name}' in {path} must be a mapping.")
    unknown = sorted(set(entry).difference(_CONFIG_FIELDS))
    if unknown:
        raise GeneratorConfigurationError(
            f"Generator '{name}' in {path} has unsupported fields: {', '.join(unknown)}"
        )
    command = entry.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise GeneratorConfigurationError(
            f"Generator '{name}' in {path} must declare a non-empty command list."
        )
    protocol = entry.get("protocol_version", GENERATOR_PROTOCOL_VERSION)
    if protocol != GENERATOR_PROTOCOL_VERSION:
        raise GeneratorConfigurationError(
            f"Generator '{name}' uses protocol {protocol!r}; supported protocol is {GENERATOR_PROTOCOL_VERSION!r}."
        )
    pass_env = entry.get("pass_env", [])
    if not isinstance(pass_env, list) or not all(isinstance(item, str) and ENV_NAME_RE.fullmatch(item) for item in pass_env):
        raise GeneratorConfigurationError(
            f"Generator '{name}' pass_env values must be environment-variable names."
        )
    timeout = _bounded_int(
        entry.get("timeout_seconds", GENERATOR_ADAPTER_TIMEOUT_SECONDS),
        minimum=1,
        maximum=MAX_GENERATOR_TIMEOUT_SECONDS,
        label=f"Generator '{name}' timeout_seconds",
    )
    idle_raw = entry.get("idle_timeout_seconds")
    idle = (
        None
        if idle_raw is None
        else _bounded_int(
            idle_raw,
            minimum=1,
            maximum=timeout,
            label=f"Generator '{name}' idle_timeout_seconds",
        )
    )
    max_request = _bounded_int(
        entry.get("max_request_bytes", DEFAULT_GENERATOR_MAX_REQUEST_BYTES),
        minimum=1,
        maximum=MAX_GENERATOR_REQUEST_BYTES,
        label=f"Generator '{name}' max_request_bytes",
    )
    return GeneratorSpec(
        name=name,
        command=tuple(_expand_executable(item) if index == 0 else item for index, item in enumerate(command)),
        pass_env=tuple(dict.fromkeys(pass_env)),
        timeout_seconds=timeout,
        idle_timeout_seconds=idle,
        max_request_bytes=max_request,
    )


def _bounded_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise GeneratorConfigurationError(f"{label} must be an integer from {minimum} through {maximum}.")
    return value


def _expand_executable(value: str) -> str:
    return str(Path(value).expanduser()) if value.startswith(("~/", "~\\")) else value


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeneratorExchangeError(f"{label} must contain exactly one JSON object.") from exc
    if not isinstance(raw, dict):
        raise GeneratorExchangeError(f"{label} must contain exactly one JSON object.")
    return raw


def _fill_content_hashes(candidate: dict[str, Any]) -> None:
    changes = candidate.get("changes")
    if not isinstance(changes, list):
        return
    for change in changes:
        if isinstance(change, dict) and isinstance(change.get("content"), str):
            change["content_sha256"] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()
