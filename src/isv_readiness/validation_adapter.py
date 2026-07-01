from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ADAPTER_CONTRACT_VERSION = "0.1.0"
DEFAULT_VALIDATION_PHASE = "test"
ADAPTER_HANDLED_CATEGORIES = {"reframe"}


class ValidationAdapterError(RuntimeError):
    """Raised when an isvctl machine-readable contract cannot be consumed."""


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    labels: tuple[str, ...]
    module: str | None
    platforms: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogSnapshot:
    version: str | None
    entries: tuple[CatalogEntry, ...]
    fingerprint: str


@dataclass(frozen=True)
class PlannedStep:
    platform: str
    name: str
    phase: str | None
    command: str | None
    skipped: bool
    valid: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedValidation:
    name: str
    base_name: str
    category: str
    step: str | None
    phase: str
    params: Any
    labels: tuple[str, ...]
    platforms: tuple[str, ...]
    description: str | None
    module: str | None
    known_to_catalog: bool
    valid: bool
    error: str | None = None
    execution_adapter: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationPlan:
    contract_version: str
    config_version: str | None
    catalog_version: str | None
    isvctl_version: str | None
    platform: str | None
    config_files: tuple[str, ...]
    config_fingerprint: str
    catalog_fingerprint: str
    features: tuple[str, ...]
    steps: tuple[PlannedStep, ...]
    validations: tuple[PlannedValidation, ...]
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CommandRunner = Callable[[Sequence[str], Path | None, int], subprocess.CompletedProcess[str]]


def normalize_catalog(payload: Mapping[str, Any]) -> CatalogSnapshot:
    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValidationAdapterError("isvctl catalog JSON field 'entries' must be a list")

    entries: list[CatalogEntry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            entries.append(
                CatalogEntry(
                    name=f"<invalid-catalog-entry-{index}>",
                    description="",
                    labels=(),
                    module=None,
                    platforms=(),
                    metadata={"error": "catalog entry must be an object", "raw": raw},
                )
            )
            continue

        known_keys = {"name", "description", "labels", "module", "platforms"}
        name = raw.get("name")
        metadata = {str(key): value for key, value in raw.items() if key not in known_keys}
        if not isinstance(name, str) or not name:
            metadata["error"] = "catalog entry name must be a non-empty string"
            name = f"<invalid-catalog-entry-{index}>"
        entries.append(
            CatalogEntry(
                name=name,
                description=raw.get("description") if isinstance(raw.get("description"), str) else "",
                labels=_string_tuple(raw.get("labels")),
                module=raw.get("module") if isinstance(raw.get("module"), str) else None,
                platforms=_string_tuple(raw.get("platforms")),
                metadata=metadata,
            )
        )

    version = payload.get("isvTestVersion")
    return CatalogSnapshot(
        version=version if isinstance(version, str) else None,
        entries=tuple(entries),
        fingerprint=_fingerprint(payload),
    )


def normalize_validation_plan(
    config: Mapping[str, Any],
    catalog: CatalogSnapshot,
    *,
    config_files: Sequence[str | Path] = (),
    isvctl_version: str | None = None,
    warnings: Sequence[str] = (),
) -> ValidationPlan:
    mutable_warnings = list(warnings)
    tests = config.get("tests", {})
    if not isinstance(tests, Mapping):
        tests = {}
        mutable_warnings.append("Merged config field 'tests' is not an object.")

    validations_config = tests.get("validations", {})
    if not isinstance(validations_config, Mapping):
        validations_config = {
            "<invalid>": {"<invalid>": {"_invalid_config": "tests.validations must be an object"}}
        }

    catalog_by_name = {entry.name: entry for entry in catalog.entries}
    validations: list[PlannedValidation] = []
    for category, category_config in validations_config.items():
        category_name = str(category)
        for name, params, group_step, group_phase, group_metadata in _iter_validation_items(
            category_name, category_config
        ):
            step = group_step if isinstance(group_step, str) else None
            phase = group_phase if isinstance(group_phase, str) else None
            if isinstance(params, Mapping):
                if step is None and isinstance(params.get("step"), str):
                    step = params["step"]
                if phase is None and isinstance(params.get("phase"), str):
                    phase = params["phase"]

            catalog_entry, base_name = _resolve_catalog_entry(name, catalog_by_name)
            error = _validation_error(category_name, name, params)
            validations.append(
                PlannedValidation(
                    name=name,
                    base_name=base_name,
                    category=category_name,
                    step=step,
                    phase=phase or DEFAULT_VALIDATION_PHASE,
                    params=params,
                    labels=catalog_entry.labels if catalog_entry else (),
                    platforms=catalog_entry.platforms if catalog_entry else (),
                    description=catalog_entry.description if catalog_entry else None,
                    module=catalog_entry.module if catalog_entry else None,
                    known_to_catalog=catalog_entry is not None,
                    valid=error is None,
                    error=error,
                    execution_adapter=category_name if category_name in ADAPTER_HANDLED_CATEGORIES else None,
                    metadata={
                        "group": group_metadata,
                        "catalog": catalog_entry.metadata if catalog_entry else {},
                    },
                )
            )

    raw_version = config.get("version")
    raw_platform = tests.get("platform")
    metadata = {
        "description": tests.get("description") if isinstance(tests.get("description"), str) else None,
        "exclude": tests.get("exclude") if isinstance(tests.get("exclude"), Mapping) else {},
        "config_extra": {
            str(key): value for key, value in config.items() if key not in {"version", "commands", "tests"}
        },
    }
    return ValidationPlan(
        contract_version=ADAPTER_CONTRACT_VERSION,
        config_version=raw_version if isinstance(raw_version, str) else None,
        catalog_version=catalog.version,
        isvctl_version=isvctl_version,
        platform=raw_platform if isinstance(raw_platform, str) else None,
        config_files=tuple(str(Path(path)) for path in config_files),
        config_fingerprint=_fingerprint(config),
        catalog_fingerprint=catalog.fingerprint,
        features=("catalog_json", "dry_run_json", "normalized_validation_shapes"),
        steps=tuple(_normalize_steps(config.get("commands"))),
        validations=tuple(validations),
        warnings=tuple(mutable_warnings),
        metadata=metadata,
    )


class IsvctlAdapter:
    """Thin adapter around stable, machine-readable isvctl CLI surfaces."""

    def __init__(
        self,
        validation_root: Path | None = None,
        *,
        executable: Sequence[str] | None = None,
        timeout_seconds: int = 120,
        runner: CommandRunner | None = None,
    ) -> None:
        self.validation_root = validation_root.resolve() if validation_root is not None else None
        if executable is not None:
            self.command_prefix = tuple(executable)
        elif self.validation_root is not None:
            self.command_prefix = ("uv", "run", "isvctl")
        else:
            self.command_prefix = ("isvctl",)
        self.timeout_seconds = timeout_seconds
        self.runner = runner or _default_runner

    def catalog(self) -> CatalogSnapshot:
        return normalize_catalog(self._run_json(("catalog", "list", "--json"), "test catalog"))

    def merged_config(self, config_files: Sequence[str | Path]) -> Mapping[str, Any]:
        if not config_files:
            raise ValidationAdapterError("at least one config file is required")
        args: list[str] = ["test", "run"]
        for config_file in config_files:
            args.extend(("-f", str(config_file)))
        args.extend(("--dry-run", "--no-upload"))
        return self._run_json(tuple(args), "dry-run config")

    def version(self) -> str:
        result = self._run(("--version",))
        output = (result.stdout or "").strip()
        if not output:
            raise ValidationAdapterError("isvctl --version returned no output")
        return output.splitlines()[-1]

    def plan(self, config_files: Sequence[str | Path]) -> ValidationPlan:
        warnings: list[str] = []
        try:
            version = self.version()
        except ValidationAdapterError as exc:
            version = None
            warnings.append(str(exc))
        catalog = self.catalog()
        config = self.merged_config(config_files)
        return normalize_validation_plan(
            config,
            catalog,
            config_files=config_files,
            isvctl_version=version,
            warnings=warnings,
        )

    def _run_json(self, args: Sequence[str], label: str) -> Mapping[str, Any]:
        result = self._run(args)
        payload = _decode_json_object(result.stdout or "", label)
        if not isinstance(payload, Mapping):
            raise ValidationAdapterError(f"isvctl {label} output must be a JSON object")
        return payload

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = (*self.command_prefix, *args)
        result = self.runner(command, self.validation_root, self.timeout_seconds)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            if len(details) > 2000:
                details = details[-2000:]
            raise ValidationAdapterError(
                f"Command {' '.join(command)} failed with exit code {result.returncode}: {details or 'no output'}"
            )
        return result


def _iter_validation_items(
    category: str, category_config: Any
) -> list[tuple[str, Any, Any, Any, dict[str, Any]]]:
    if isinstance(category_config, Mapping) and "checks" in category_config:
        group_step = category_config.get("step")
        group_phase = category_config.get("phase")
        group_metadata = {
            str(key): value for key, value in category_config.items() if key not in {"checks", "step", "phase"}
        }
        checks = category_config.get("checks")
        if isinstance(checks, Mapping):
            return [
                (str(name), params if params is not None else {}, group_step, group_phase, group_metadata)
                for name, params in checks.items()
            ]
        if isinstance(checks, list):
            return _expand_check_list(
                checks,
                group_step,
                group_phase,
                group_metadata,
                f"each item in category '{category}.checks' must be an object",
            )
        return [
            (
                "<invalid>",
                {"_invalid_config": f"checks for category '{category}' must be an object or list"},
                None,
                None,
                group_metadata,
            )
        ]

    if isinstance(category_config, list):
        return _expand_check_list(
            category_config,
            None,
            None,
            {},
            f"each item in category '{category}' must be an object",
        )

    if isinstance(category_config, Mapping):
        return [
            (str(name), params if params is not None else {}, None, None, {})
            for name, params in category_config.items()
        ]

    return [
        (
            "<invalid>",
            {"_invalid_config": f"category '{category}' validations must be an object or list"},
            None,
            None,
            {},
        )
    ]


def _expand_check_list(
    items: list[Any],
    group_step: Any,
    group_phase: Any,
    group_metadata: dict[str, Any],
    invalid_message: str,
) -> list[tuple[str, Any, Any, Any, dict[str, Any]]]:
    result: list[tuple[str, Any, Any, Any, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            result.append(
                ("<invalid>", {"_invalid_config": invalid_message, "raw": item}, None, None, group_metadata)
            )
            continue
        for name, params in item.items():
            result.append(
                (
                    str(name),
                    params if params is not None else {},
                    group_step,
                    group_phase,
                    group_metadata,
                )
            )
    return result


def _normalize_steps(commands: Any) -> list[PlannedStep]:
    if not isinstance(commands, Mapping):
        return []
    result: list[PlannedStep] = []
    for platform, platform_config in commands.items():
        if not isinstance(platform_config, Mapping):
            result.append(
                PlannedStep(
                    platform=str(platform),
                    name="<invalid>",
                    phase=None,
                    command=None,
                    skipped=False,
                    valid=False,
                    error=f"commands.{platform} must be an object",
                )
            )
            continue
        raw_steps = platform_config.get("steps", [])
        if not isinstance(raw_steps, list):
            result.append(
                PlannedStep(
                    platform=str(platform),
                    name="<invalid>",
                    phase=None,
                    command=None,
                    skipped=False,
                    valid=False,
                    error=f"commands.{platform}.steps must be a list",
                )
            )
            continue
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, Mapping) or not isinstance(raw_step.get("name"), str):
                result.append(
                    PlannedStep(
                        platform=str(platform),
                        name=f"<invalid-{index}>",
                        phase=None,
                        command=None,
                        skipped=False,
                        valid=False,
                        error=f"commands.{platform}.steps[{index}] must have a string name",
                        metadata={"raw": raw_step},
                    )
                )
                continue
            known_keys = {"name", "phase", "command", "skip"}
            result.append(
                PlannedStep(
                    platform=str(platform),
                    name=raw_step["name"],
                    phase=raw_step.get("phase") if isinstance(raw_step.get("phase"), str) else None,
                    command=raw_step.get("command") if isinstance(raw_step.get("command"), str) else None,
                    skipped=bool(raw_step.get("skip")),
                    metadata={str(key): value for key, value in raw_step.items() if key not in known_keys},
                )
            )
    return result


def _resolve_catalog_entry(
    name: str, catalog_by_name: Mapping[str, CatalogEntry]
) -> tuple[CatalogEntry | None, str]:
    variant_bases = [candidate for candidate in catalog_by_name if name.startswith(f"{candidate}-")]
    base_name = max(variant_bases, key=len) if variant_bases else name
    entry = catalog_by_name.get(name) or catalog_by_name.get(base_name)
    if entry is None and "-" in name:
        base_name = name.split("-", 1)[0]
        entry = catalog_by_name.get(base_name)
    return entry, base_name


def _validation_error(category: str, name: str, params: Any) -> str | None:
    if not name:
        return f"category '{category}' contains an empty validation name"
    if not isinstance(params, Mapping):
        return f"validation '{name}' parameters must be an object"
    invalid_message = params.get("_invalid_config")
    return str(invalid_message) if invalid_message else None


def _decode_json_object(text: str, label: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(stripped[start:])
                return value
            except json.JSONDecodeError:
                pass
    raise ValidationAdapterError(f"isvctl {label} did not emit valid JSON")


def _default_runner(
    command: Sequence[str], cwd: Path | None, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_seconds,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
