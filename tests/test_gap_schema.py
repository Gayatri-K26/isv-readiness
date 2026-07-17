from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.schema import load_schema

ROOT = Path(__file__).resolve().parents[1]


class GapSchemaTests(unittest.TestCase):
    def test_sample_report_matches_schema(self) -> None:
        schema = load_schema("gaps.schema.json")
        sample = json.loads((ROOT / "examples" / "gaps.sample.json").read_text(encoding="utf-8"))

        jsonschema.Draft202012Validator(schema).validate(sample)
        self.assertEqual(sample["schema_version"], "0.2.0")

        sample["rows"][0]["milestone"] = "M0"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(sample)


if __name__ == "__main__":
    unittest.main()
