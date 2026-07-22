from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from isv_readiness.scan.models import (
    SCHEMA_VERSION,
    Evidence,
    GapReport,
    GapRow,
    Remediation,
    Stage,
    Status,
)
from isv_readiness.scan.schema_registry import SchemaRegistry
from isv_readiness.validation_adapter import normalize_catalog, normalize_validation_plan

K8S_DOMAINS = {"k8s", "kubernetes"}
VALIDATION_ONLY_STEP = "<validation>"
VALIDATION_CONFIG_STEP = "<validation-config>"


@dataclass(frozen=True)
class ScanOptions:
    provider_repo: Path
    domains: list[str]
    validation_root: Path | None = None


@dataclass(frozen=True)
class StepRef:
    name: str
    phase: str | None
    command: str | None
    skipped: bool
    config_path: Path
    script_path: Path | None
    required_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckRef:
    step_name: str
    validation_class: str
    requirement_id: str | None = None
    requires_step: bool = True
    valid: bool = True
    error: str | None = None
    enrichment: dict[str, Any] | None = None


def scan_provider(options: ScanOptions) -> GapReport:
    provider_repo = options.provider_repo.resolve()
    validation_root = _resolve_validation_root(provider_repo, options.validation_root)
    registry = SchemaRegistry(validation_root)
    rows: list[GapRow] = []

    for domain in options.domains:
        config_path = _find_domain_config(provider_repo, domain)
        if config_path is None:
            is_k8s = _is_k8s_domain(domain)
            rows.append(
                _row(
                    provider_repo=provider_repo,
                    domain=domain,
                    step_name="<config>",
                    validation_class=None,
                    requirement_id=None,
                    status="not_implemented" if is_k8s else "error",
                    stage="coverage",
                    message=_missing_config_message(provider_repo, domain),
                    config_path=None,
                    script_path=None,
                    target=_suggest_config_target(provider_repo, domain),
                    aws_reference=None,
                    schema_errors=[],
                    missing_json_fields=[],
                    auto_fixable=is_k8s,
                    rerun_config=None,
                )
            )
            continue

        provider_config = _read_yaml(config_path)
        suite_docs = _load_imported_suite_docs(config_path, provider_config, domain, validation_root)
        steps = _steps_for_domain(provider_config, domain, config_path)
        checks = _checks_for_domain(suite_docs, provider_config)
        aws_steps = _aws_steps_for_domain(validation_root, domain)

        step_names_with_checks = {check.step_name for check in checks if check.requires_step}
        for step_name in steps:
            if step_name not in step_names_with_checks:
                checks.append(CheckRef(step_name=step_name, validation_class="StepOutputSchema"))

        for check in _checks_in_execution_order(
            checks,
            steps,
            _phases_for_domain(provider_config, domain),
        ):
            step = steps.get(check.step_name)
            aws_step = aws_steps.get(check.step_name)
            aws_reference = _relative_or_str(aws_step.script_path, provider_repo) if aws_step else None
            rows.append(
                _scan_check(
                    provider_repo=provider_repo,
                    domain=domain,
                    check=check,
                    step=step,
                    config_path=config_path,
                    registry=registry,
                    aws_reference=aws_reference,
                )
            )

    return GapReport(
        schema_version=SCHEMA_VERSION,
        provider_repo=str(provider_repo),
        domains=options.domains,
        rows=rows,
    )


def _scan_check(
    *,
    provider_repo: Path,
    domain: str,
    check: CheckRef,
    step: StepRef | None,
    config_path: Path,
    registry: SchemaRegistry,
    aws_reference: str | None,
) -> GapRow:
    rerun_config = config_path
    if not check.valid:
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="error",
            stage="coverage",
            message=f"Validation configuration is invalid: {check.error or 'unknown contract error'}",
            config_path=config_path,
            script_path=None,
            target=_relative_or_str(config_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=False,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if not check.requires_step:
        phase = (check.enrichment or {}).get("validation_phase", "test")
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="pass",
            stage="coverage",
            message=(
                f"Validation is declared for phase '{phase}' without a provider step binding; "
                "its runtime outcome is deferred to ai-cloud-validation."
            ),
            config_path=config_path,
            script_path=None,
            target=None,
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=False,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if step is None:
        # Wiring the step lives in the selected domain's own config — inside
        # the guarded fix surface — so the agent may draft it; profile routing
        # still decides whether this row is the ISV's to implement at all.
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="not_implemented",
            stage="coverage",
            message="Provider config does not wire a command for this required step.",
            config_path=config_path,
            script_path=None,
            target=_relative_or_str(config_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=True,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if step.skipped:
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="skipped",
            stage="coverage",
            message="Provider config marks this step as skipped.",
            config_path=step.config_path,
            script_path=step.script_path,
            target=None,
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=False,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if _is_template_k8s_command(provider_repo, step):
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="not_implemented",
            stage="coverage",
            message="Kubernetes command still points at the my-isv template scripts instead of this provider.",
            config_path=step.config_path,
            script_path=step.script_path,
            target=_relative_or_str(step.config_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=True,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if step.script_path is None:
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="error",
            stage="coverage",
            message="Command does not reference a .py or .sh provider script.",
            config_path=step.config_path,
            script_path=None,
            target=_relative_or_str(step.config_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=False,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    if not step.script_path.exists():
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="not_implemented",
            stage="coverage",
            message="Provider script referenced by config is missing.",
            config_path=step.config_path,
            script_path=step.script_path,
            target=_relative_or_str(step.script_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=_is_script_target(provider_repo, step.script_path),
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    text = step.script_path.read_text(encoding="utf-8")
    syntax_error = _python_syntax_error(step.script_path, text)
    if syntax_error is not None:
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="error",
            stage="coverage",
            message=f"Provider script has invalid Python syntax: {syntax_error}",
            config_path=step.config_path,
            script_path=step.script_path,
            target=_relative_or_str(step.script_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=_is_script_target(provider_repo, step.script_path),
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )
    stub_hit = _stub_marker(step.script_path, text)
    if stub_hit is not None:
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="not_implemented",
            stage="coverage",
            message=f"Provider script still contains stub marker: {stub_hit}",
            config_path=step.config_path,
            script_path=step.script_path,
            target=_relative_or_str(step.script_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=_is_script_target(provider_repo, step.script_path),
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    schema_name = registry.schema_for_step(step.name)
    empty_outputs = _definitely_empty_output_fields(
        step.script_path,
        text,
        set(step.required_outputs),
    )
    if empty_outputs:
        problems = [f"Required output field '{field}' is never populated." for field in empty_outputs]
        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="fail",
            stage="correctness",
            message="Provider script leaves required step outputs definitely empty.",
            config_path=step.config_path,
            script_path=step.script_path,
            target=_relative_or_str(step.script_path, provider_repo),
            aws_reference=aws_reference,
            schema_errors=problems,
            missing_json_fields=[],
            auto_fixable=_is_script_target(provider_repo, step.script_path),
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    output_samples = _static_json_outputs(step.script_path, text)
    if output_samples and schema_name:
        all_errors: list[str] = []
        all_missing: list[str] = []
        for sample in output_samples:
            _valid, schema_errors, missing_fields = registry.validate_output(sample, schema_name)
            all_errors.extend(schema_errors)
            all_missing.extend(missing_fields)
        if all_errors:
            return _row(
                provider_repo=provider_repo,
                domain=domain,
                step_name=check.step_name,
                validation_class=check.validation_class,
                requirement_id=check.requirement_id,
                status="fail",
                stage="correctness",
                message=f"Static JSON sample does not match expected '{schema_name}' output schema.",
                config_path=step.config_path,
                script_path=step.script_path,
                target=_relative_or_str(step.script_path, provider_repo),
                aws_reference=aws_reference,
                schema_errors=all_errors,
                missing_json_fields=sorted(set(all_missing)),
                auto_fixable=_is_script_target(provider_repo, step.script_path),
                rerun_config=rerun_config,
                enrichment=check.enrichment,
            )

        return _row(
            provider_repo=provider_repo,
            domain=domain,
            step_name=check.step_name,
            validation_class=check.validation_class,
            requirement_id=check.requirement_id,
            status="pass",
            stage="coverage",
            message=f"Static JSON sample matches expected '{schema_name}' output schema.",
            config_path=step.config_path,
            script_path=step.script_path,
            target=None,
            aws_reference=aws_reference,
            schema_errors=[],
            missing_json_fields=[],
            auto_fixable=False,
            rerun_config=rerun_config,
            enrichment=check.enrichment,
        )

    return _row(
        provider_repo=provider_repo,
        domain=domain,
        step_name=check.step_name,
        validation_class=check.validation_class,
        requirement_id=check.requirement_id,
        status="pass",
        stage="coverage",
        message="No static stub markers found; output-schema validation is deferred to dynamic ai-cloud-validation runs.",
        config_path=step.config_path,
        script_path=step.script_path,
        target=None,
        aws_reference=aws_reference,
        schema_errors=[],
        missing_json_fields=[],
        auto_fixable=False,
        rerun_config=rerun_config,
        enrichment=check.enrichment,
    )


def _row(
    *,
    provider_repo: Path,
    domain: str,
    step_name: str,
    validation_class: str | None,
    requirement_id: str | None,
    status: Status,
    stage: Stage,
    message: str,
    config_path: Path | None,
    script_path: Path | None,
    target: str | None,
    aws_reference: str | None,
    schema_errors: list[str],
    missing_json_fields: list[str],
    auto_fixable: bool,
    rerun_config: Path | None,
    enrichment: dict[str, Any] | None = None,
) -> GapRow:
    validation_instance = str((enrichment or {}).get("validation_instance", ""))
    spine = "|".join([domain, step_name, validation_class or "", requirement_id or ""])
    if validation_instance:
        spine = f"{spine}|{validation_instance}"
    gap_id = "gap_" + hashlib.sha1(spine.encode("utf-8")).hexdigest()[:12]
    rerun_command = (
        f"isvctl test run -f {_relative_or_str(rerun_config, provider_repo)}"
        if rerun_config
        else "isvctl test run -f <provider-config>"
    )
    return GapRow(
        id=gap_id,
        domain=domain,
        step_name=step_name,
        validation_class=validation_class,
        requirement_id=requirement_id,
        status=status,
        detection="static",
        stage=stage,
        evidence=Evidence(
            message=message,
            validation_message=None,
            schema_errors=schema_errors,
            missing_json_fields=missing_json_fields,
            stderr_excerpt=None,
            script_path=_relative_or_str(script_path, provider_repo),
            config_path=_relative_or_str(config_path, provider_repo),
        ),
        remediation=Remediation(
            auto_fixable=auto_fixable,
            target=target,
            rerun_command=rerun_command,
            aws_reference=aws_reference,
        ),
        enrichment=enrichment or {},
        labels=tuple(
            label
            for label in (enrichment or {}).get("upstream_labels", [])
            if isinstance(label, str)
        ),
    )


def _resolve_validation_root(provider_repo: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    candidates = [
        provider_repo.parent / "ai-cloud-validation",
        provider_repo.parent.parent / "ai-cloud-validation",
        Path.cwd().parent / "ai-cloud-validation",
    ]
    for candidate in candidates:
        if (candidate / "isvctl" / "configs" / "suites").exists():
            return candidate.resolve()
    return None


def _find_domain_config(provider_repo: Path, domain: str) -> Path | None:
    if provider_repo.is_file() and provider_repo.suffix in {".yaml", ".yml"}:
        return provider_repo.resolve()

    names = _domain_config_names(domain)
    candidates: list[Path] = []
    for name in names:
        candidates.extend([provider_repo / "config" / name, provider_repo / name])

    if _is_k8s_domain(domain):
        candidates.extend([
            provider_repo.with_suffix(".yaml"),
            provider_repo.with_suffix(".yml"),
            provider_repo.parent / f"{provider_repo.name}.yaml",
            provider_repo.parent / f"{provider_repo.name}.yml",
        ])

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate.resolve()
    return None


def _is_k8s_domain(domain: str) -> bool:
    return domain in K8S_DOMAINS


def _domain_config_names(domain: str) -> list[str]:
    if _is_k8s_domain(domain):
        return ["k8s.yaml", "k8s.yml", "kubernetes.yaml", "kubernetes.yml"]
    return [f"{domain}.yaml", f"{domain}.yml"]


def _suite_file_name(domain: str) -> str:
    return "k8s.yaml" if _is_k8s_domain(domain) else f"{domain}.yaml"


def _command_keys_for_domain(domain: str) -> list[str]:
    if _is_k8s_domain(domain):
        return ["kubernetes", "k8s"]
    return [domain]


def _suggest_config_target(provider_repo: Path, domain: str) -> str | None:
    if not _is_k8s_domain(domain):
        return None
    if provider_repo.suffix in {".yaml", ".yml"}:
        return str(provider_repo)
    return str(provider_repo.with_suffix(".yaml"))


def _missing_config_message(provider_repo: Path, domain: str) -> str:
    if not _is_k8s_domain(domain):
        return f"No provider config found for domain '{domain}'"
    target = _suggest_config_target(provider_repo, domain) or "isvctl/configs/providers/<provider>.yaml"
    return (
        "No Kubernetes provider wrapper found. Create a provider config that imports "
        f"suites/k8s.yaml and points setup/teardown at this provider's scripts, for example {target}."
    )


def _is_template_k8s_command(provider_repo: Path, step: StepRef) -> bool:
    if provider_repo.name == "my-isv" or not step.command:
        return False
    normalized = step.command.replace("\\", "/")
    return "my-isv/scripts/k8s/" in normalized or "providers/my-isv/scripts/k8s/" in normalized


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_imported_suite_docs(
    config_path: Path,
    provider_config: dict[str, Any],
    domain: str,
    validation_root: Path | None,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    imports = provider_config.get("import") or []
    if isinstance(imports, str):
        imports = [imports]
    for imported in imports:
        if not isinstance(imported, str):
            continue
        path = (config_path.parent / imported).resolve()
        if not path.exists() and validation_root is not None:
            fallback = validation_root / "isvctl" / "configs" / "suites" / Path(imported).name
            if fallback.exists():
                path = fallback.resolve()
        if path.exists():
            docs.append(_read_yaml(path))

    if not docs and validation_root is not None:
        fallback = validation_root / "isvctl" / "configs" / "suites" / _suite_file_name(domain)
        if fallback.exists():
            docs.append(_read_yaml(fallback))
    return docs


def _checks_for_domain(suite_docs: list[dict[str, Any]], provider_config: dict[str, Any]) -> list[CheckRef]:
    merged_config: dict[str, Any] = {}
    for document in [*suite_docs, provider_config]:
        merged_config = _deep_merge_config(merged_config, document)

    plan = normalize_validation_plan(
        merged_config,
        normalize_catalog({"entries": []}),
    )
    checks: list[CheckRef] = []
    for validation in plan.validations:
        requires_step = validation.step is not None
        params = validation.params if isinstance(validation.params, dict) else {}
        raw_test_id = params.get("test_id")
        requirement_id = None
        if isinstance(raw_test_id, str):
            normalized_test_id = raw_test_id.strip()
            if normalized_test_id and normalized_test_id != "N/A":
                requirement_id = normalized_test_id
        raw_labels = params.get("labels")
        candidate_labels = (
            tuple(label for label in raw_labels if isinstance(label, str))
            if isinstance(raw_labels, (list, tuple))
            else validation.labels
        )
        labels = tuple(dict.fromkeys(candidate_labels))
        enrichment: dict[str, Any] = {
            "validation_category": validation.category,
            "validation_phase": validation.phase,
            "requires_provider_step": requires_step,
            "upstream_labels": list(labels),
        }
        if validation.execution_adapter is not None:
            enrichment["execution_adapter"] = validation.execution_adapter
        checks.append(
            CheckRef(
                step_name=validation.step or VALIDATION_ONLY_STEP,
                validation_class=validation.name,
                requirement_id=requirement_id,
                requires_step=requires_step,
                valid=validation.valid,
                error=validation.error,
                enrichment=enrichment,
            )
        )

    for warning in plan.warnings:
        checks.append(
            CheckRef(
                step_name=VALIDATION_CONFIG_STEP,
                validation_class="ValidationConfigContract",
                requires_step=False,
                valid=False,
                error=warning,
                enrichment={
                    "validation_category": "<contract>",
                    "validation_phase": "configuration",
                    "requires_provider_step": False,
                },
            )
        )

    identities = Counter((check.step_name, check.validation_class) for check in checks)
    occurrences: Counter[tuple[str, str]] = Counter()
    result: list[CheckRef] = []
    for check in checks:
        identity = (check.step_name, check.validation_class)
        if identities[identity] == 1:
            result.append(check)
            continue
        occurrences[identity] += 1
        enrichment = dict(check.enrichment or {})
        category = enrichment.get("validation_category", "validation")
        enrichment["validation_instance"] = f"{category}:{occurrences[identity]}"
        result.append(replace(check, enrichment=enrichment))
    return result


def _deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Mirror isvctl's mapping merge and list-replacement behavior for static scans."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _checks_in_execution_order(
    checks: list[CheckRef],
    steps: dict[str, StepRef],
    declared_phases: tuple[str, ...],
) -> list[CheckRef]:
    """Keep gap work aligned with the provider's declared execution plan."""

    phase_names = list(dict.fromkeys(("configuration", *declared_phases)))
    for step in steps.values():
        if step.phase and step.phase not in phase_names:
            phase_names.append(step.phase)
    for check in checks:
        phase = (check.enrichment or {}).get("validation_phase")
        if isinstance(phase, str) and phase not in phase_names:
            phase_names.append(phase)

    phase_positions = {phase: index for index, phase in enumerate(phase_names)}
    step_positions = {name: index for index, name in enumerate(steps)}

    def key(indexed_check: tuple[int, CheckRef]) -> tuple[int, int, int, int]:
        original_index, check = indexed_check
        step = steps.get(check.step_name)
        raw_phase = step.phase if step and step.phase else (check.enrichment or {}).get("validation_phase")
        phase = raw_phase if isinstance(raw_phase, str) else ""
        phase_position = phase_positions.get(phase, len(phase_positions))

        if check.step_name in step_positions:
            return phase_position, 0, step_positions[check.step_name], original_index

        # A suite-required step that the provider has not wired yet has no
        # provider execution position. Keep it in upstream validation order,
        # after the configured work in the same phase.
        return phase_position, 1, original_index, original_index

    return [check for _, check in sorted(enumerate(checks), key=key)]


def _command_entries_for_domain(
    provider_config: dict[str, Any], domain: str
) -> list[dict[str, Any]]:
    commands = provider_config.get("commands") or {}
    if not isinstance(commands, dict):
        return []
    command_keys = _command_keys_for_domain(domain)
    matched_entries = [commands[key] for key in command_keys if isinstance(commands.get(key), dict)]
    if matched_entries:
        return matched_entries
    return [entry for entry in commands.values() if isinstance(entry, dict)]


def _phases_for_domain(provider_config: dict[str, Any], domain: str) -> tuple[str, ...]:
    phases: list[str] = []
    for entry in _command_entries_for_domain(provider_config, domain):
        raw_phases = entry.get("phases") or []
        if not isinstance(raw_phases, list):
            continue
        for phase in raw_phases:
            if isinstance(phase, str) and phase not in phases:
                phases.append(phase)
    return tuple(phases)


def _steps_for_domain(provider_config: dict[str, Any], domain: str, config_path: Path) -> dict[str, StepRef]:
    output_references = _step_output_references(provider_config)
    steps: dict[str, StepRef] = {}
    for entry in _command_entries_for_domain(provider_config, domain):
        raw_steps = entry.get("steps") or []
        if not isinstance(raw_steps, list):
            continue
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict) or not isinstance(raw_step.get("name"), str):
                continue
            command = raw_step.get("command")
            script_path = _resolve_script_path(config_path, command) if isinstance(command, str) else None
            steps[raw_step["name"]] = StepRef(
                name=raw_step["name"],
                phase=raw_step.get("phase") if isinstance(raw_step.get("phase"), str) else None,
                command=command if isinstance(command, str) else None,
                skipped=bool(raw_step.get("skip")),
                config_path=config_path,
                script_path=script_path,
                required_outputs=tuple(sorted(output_references.get(raw_step["name"], set()))),
            )
    return steps


def _step_output_references(raw: Any) -> dict[str, set[str]]:
    """Return fields consumed through ``steps.<step>.<field>`` templates."""

    references: dict[str, set[str]] = {}
    pattern = re.compile(r"\{\{\s*steps\.([A-Za-z0-9_-]+)\.([A-Za-z_][A-Za-z0-9_]*)")
    for value in _nested_strings(raw):
        for step, field in pattern.findall(value):
            references.setdefault(step, set()).add(field)
    return references


def _nested_strings(raw: Any):
    if isinstance(raw, str):
        yield raw
    elif isinstance(raw, dict):
        # A skipped command step is never executed, so its arguments cannot
        # create a runtime dependency on outputs from an earlier step.
        if (
            raw.get("skip") is True
            and isinstance(raw.get("name"), str)
            and ("command" in raw or "args" in raw)
        ):
            return
        for value in raw.values():
            yield from _nested_strings(value)
    elif isinstance(raw, (list, tuple)):
        for value in raw:
            yield from _nested_strings(value)


def _aws_steps_for_domain(validation_root: Path | None, domain: str) -> dict[str, StepRef]:
    if validation_root is None:
        return {}
    config_path = validation_root / "isvctl" / "configs" / "providers" / "aws" / "config" / f"{domain}.yaml"
    if not config_path.exists():
        return {}
    return _steps_for_domain(_read_yaml(config_path), domain, config_path)


def _resolve_script_path(config_path: Path, command: str) -> Path | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    script = next((token for token in tokens if token.endswith((".py", ".sh"))), None)
    if script is None:
        return None
    return (config_path.parent / script).resolve()


def _python_syntax_error(path: Path, text: str) -> str | None:
    if path.suffix != ".py":
        return None
    try:
        ast.parse(text)
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return f"{exc.msg} ({location})"
    return None


def _stub_marker(path: Path, text: str) -> str | None:
    """Detect executable scaffolding without treating comments as behavior."""

    if path.suffix != ".py":
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            if "gapctl_stub" in lowered or "not implemented" in lowered or "not_implemented" in lowered:
                return stripped[:120]
        return None

    tree = ast.parse(text)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and _is_not_implemented_error(node.exc):
            return "raise NotImplementedError"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            lowered = node.value.lower()
            if "gapctl_stub" in lowered or "not implemented" in lowered or "not_implemented" in lowered:
                return node.value.strip()[:120]
    return None


def _is_not_implemented_error(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Name) and node.id == "NotImplementedError"


def _static_json_outputs(path: Path, text: str) -> list[dict[str, Any]]:
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    assignments: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = _literal_dict(node.value, assignments)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = value

    outputs: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        if call_name not in {"print", "write"}:
            continue
        for arg in node.args:
            value = _jsonish_arg(arg, assignments)
            if value is not None:
                outputs.append(value)
    return outputs


def _definitely_empty_output_fields(path: Path, text: str, required_fields: set[str]) -> list[str]:
    """Find downstream fields a recognizable result mapping omits or leaves empty."""

    if path.suffix != ".py" or not required_fields:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    mappings: dict[str, dict[str, list[ast.AST]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                    fields = mappings.setdefault(target.id, {})
                    _record_dict_assignments(fields, node.value)
                else:
                    assigned = _named_subscript(target)
                    if assigned is not None:
                        name, field = assigned
                        mappings.setdefault(name, {}).setdefault(field, []).append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Dict):
                fields = mappings.setdefault(node.target.id, {})
                _record_dict_assignments(fields, node.value)
            elif node.value is not None:
                assigned = _named_subscript(node.target)
                if assigned is not None:
                    name, field = assigned
                    mappings.setdefault(name, {}).setdefault(field, []).append(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr == "update"
        ):
            fields = mappings.setdefault(node.func.value.id, {})
            if node.args and isinstance(node.args[0], ast.Dict):
                _record_dict_assignments(fields, node.args[0])
            for keyword in node.keywords:
                if keyword.arg:
                    fields.setdefault(keyword.arg, []).append(keyword.value)

    emitted: list[dict[str, list[ast.AST]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in {"print", "write"}:
            continue
        for arg in node.args:
            fields = _emitted_mapping(arg, mappings)
            if fields is not None:
                emitted.append(fields)

    declared_fields, dynamic_shape = _declared_result_fields(tree, mappings)
    empty: set[str] = set()
    if declared_fields and not dynamic_shape:
        empty.update(required_fields.difference(declared_fields))
    for fields in emitted:
        success_values = fields.get("success", [])
        if success_values and all(_is_definitely_false(value) for value in success_values):
            continue
        for field in required_fields:
            values = fields.get(field, [])
            if values and all(_is_definitely_empty(value) for value in values):
                empty.add(field)
    return sorted(empty)


def _declared_result_fields(
    tree: ast.AST,
    mappings: dict[str, dict[str, list[ast.AST]]],
) -> tuple[set[str], bool]:
    """Return literal result fields and whether dynamic construction prevents proof."""

    markers = {"success", "platform"}
    candidate_names = {
        name
        for name, fields in mappings.items()
        if markers.intersection(fields)
    }
    declared = {
        field
        for name in candidate_names
        for field in mappings[name]
    }
    dynamic = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if markers.intersection(keys):
                declared.update(keys)
                dynamic = dynamic or any(
                    not isinstance(key, ast.Constant) or not isinstance(key.value, str)
                    for key in node.keys
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
                    continue
                if target.value.id in candidate_names and _constant_string(target.slice) is None:
                    dynamic = True
        elif isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in {"asdict", "model_dump", "to_dict", "vars"}:
                dynamic = True
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in candidate_names
                and node.func.attr == "update"
                and (
                    any(not isinstance(argument, ast.Dict) for argument in node.args)
                    or any(keyword.arg is None for keyword in node.keywords)
                )
            ):
                dynamic = True
    return declared, dynamic


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _record_dict_assignments(fields: dict[str, list[ast.AST]], node: ast.Dict) -> None:
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            fields.setdefault(key.value, []).append(value)


def _emitted_mapping(
    node: ast.AST,
    mappings: dict[str, dict[str, list[ast.AST]]],
) -> dict[str, list[ast.AST]] | None:
    if isinstance(node, ast.Call) and _call_name(node.func) in {"json.dumps", "dumps"} and node.args:
        node = node.args[0]
    if isinstance(node, ast.Name):
        return mappings.get(node.id)
    if isinstance(node, ast.Dict):
        fields: dict[str, list[ast.AST]] = {}
        _record_dict_assignments(fields, node)
        return fields
    return None


def _named_subscript(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
        return None
    key = node.slice
    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
        return None
    return node.value.id, key.value


def _is_definitely_empty(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or node.value == ""
    return False


def _is_definitely_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _jsonish_arg(node: ast.AST, assignments: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    direct = _literal_dict(node, assignments)
    if direct is not None:
        return direct
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = json.loads(node.value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(node, ast.Call) and _call_name(node.func) in {"json.dumps", "dumps"} and node.args:
        return _literal_dict(node.args[0], assignments)
    return None


def _literal_dict(node: ast.AST, assignments: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if isinstance(node, ast.Name):
        return assignments.get(node.id)
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _relative_or_str(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _is_script_target(provider_repo: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(provider_repo.resolve())
    except ValueError:
        return False
    return len(relative.parts) >= 2 and relative.parts[0] == "scripts"
