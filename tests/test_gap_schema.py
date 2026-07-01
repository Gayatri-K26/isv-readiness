from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


class GapSchemaTests(unittest.TestCase):
    def test_sample_report_matches_schema(self) -> None:
        schema = json.loads((ROOT / "schemas" / "gaps.schema.json").read_text(encoding="utf-8"))
        sample = json.loads((ROOT / "examples" / "gaps.sample.json").read_text(encoding="utf-8"))

        jsonschema.Draft202012Validator(schema).validate(sample)


if __name__ == "__main__":
    unittest.main()
