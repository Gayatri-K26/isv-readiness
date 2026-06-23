from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema


FALLBACK_STEP_SCHEMA_MAPPING: dict[str, str | None] = {
    "launch_instance": "instance",
    "create_instance": "instance",
    "describe_instance": "instance",
    "list_instances": "instance_list",
    "create_vpc": "network",
    "teardown": "teardown",
    "teardown_nim": "teardown",
    "deploy_nim": "generic",
}

COMMON_PROPERTIES = {
    "success": {"type": "boolean"},
    "platform": {"type": "string"},
}

FALLBACK_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "generic": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": COMMON_PROPERTIES,
        "additionalProperties": True,
    },
    "instance": {
        "type": "object",
        "required": ["success", "platform", "instance_id"],
        "properties": {
            **COMMON_PROPERTIES,
            "instance_id": {"type": "string"},
            "state": {"type": "string"},
            "public_ip": {"type": ["string", "null"]},
            "private_ip": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
    "instance_list": {
        "type": "object",
        "required": ["success", "platform", "instances"],
        "properties": {
            **COMMON_PROPERTIES,
            "instances": {"type": "array"},
            "count": {"type": "integer"},
        },
        "additionalProperties": True,
    },
    "network": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "network_id": {"type": "string"},
            "cidr": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "teardown": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": COMMON_PROPERTIES,
        "additionalProperties": True,
    },
}


@contextmanager
def _temporary_path(path: Path | None) -> Iterable[None]:
    if path is None:
        yield
        return
    path_text = str(path)
    added = path_text not in sys.path
    if added:
        sys.path.insert(0, path_text)
    try:
        yield
    finally:
        if added:
            sys.path.remove(path_text)


class SchemaRegistry:
    """Small adapter over ai-cloud-validation's schema registry.

    The scanner can run with only the fallback registry in tests or offline
    environments, but it will prefer the sibling/installed isvctl registry when
    available.
    """

    def __init__(self, validation_root: Path | None = None) -> None:
        module_path = validation_root / "isvctl" / "src" if validation_root else None
        with _temporary_path(module_path):
            try:
                self._module = importlib.import_module("isvctl.config.output_schemas")
            except Exception:
                self._module = None

    def schema_for_step(self, step_name: str) -> str | None:
        if self._module is not None:
            return self._module.get_schema_for_step(step_name)
        if step_name in FALLBACK_STEP_SCHEMA_MAPPING:
            return FALLBACK_STEP_SCHEMA_MAPPING[step_name]
        for key, schema in FALLBACK_STEP_SCHEMA_MAPPING.items():
            if key in step_name:
                return schema
        return "generic"

    def schema(self, schema_name: str) -> dict[str, Any] | None:
        if self._module is not None:
            return self._module.get_schema(schema_name)
        return FALLBACK_OUTPUT_SCHEMAS.get(schema_name)

    def validate_output(self, output: dict[str, Any], schema_name: str) -> tuple[bool, list[str], list[str]]:
        schema = self.schema(schema_name)
        if schema is None:
            return False, [f"Unknown output schema: {schema_name}"], []

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(output), key=lambda error: list(error.path))
        messages = [f"{error.json_path}: {error.message}" for error in errors]
        missing_fields: list[str] = []
        for error in errors:
            if error.validator == "required":
                missing_fields.extend(str(field) for field in error.validator_value if field not in error.instance)
        return not errors, messages, sorted(set(missing_fields))
