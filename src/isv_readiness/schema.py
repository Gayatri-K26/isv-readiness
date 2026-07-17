from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def schema_path(name: str) -> Path:
    """Return a packaged schema path without allowing path traversal."""
    if Path(name).name != name:
        raise ValueError(f"Schema name must be a filename: {name}")
    path = Path(__file__).resolve().parent / "schemas" / name
    if not path.is_file():
        raise FileNotFoundError(f"Packaged schema not found: {name}")
    return path


def load_schema(name: str) -> dict[str, Any]:
    """Load one packaged JSON schema."""
    raw = json.loads(schema_path(name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Schema must contain a JSON object: {name}")
    return raw
