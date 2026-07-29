from __future__ import annotations

import hashlib
import unittest

from isv_readiness.agent_skill import SKILL_NAME, with_agent_skill


class AgentSkillTests(unittest.TestCase):
    def test_attaches_pinned_phase_specific_skill(self) -> None:
        request = {"task": "draft"}

        qualification = with_agent_skill(request, "qualification")
        remediation = with_agent_skill(request, "remediation")

        self.assertNotIn("agent_skill", request)
        self.assertEqual(qualification["agent_skill"]["name"], SKILL_NAME)
        self.assertEqual(qualification["agent_skill"]["phase"], "qualification")
        qualification_text = " ".join(qualification["agent_skill"]["instructions"].split())
        self.assertIn("API specification is useful but not required", qualification_text)
        self.assertIn("minimum prerequisites with verified inventory", qualification_text)
        remediation_text = " ".join(remediation["agent_skill"]["instructions"].split())
        self.assertIn("smallest provider-owned change", remediation_text)
        self.assertIn("complete wire contract", remediation_text)
        self.assertIn("Model lifecycle work as a state transition", remediation_text)
        self.assertIn("must not change or disable the authenticated peer identity", remediation_text)
        self.assertIn("never scaffold defaults", remediation_text)
        self.assertNotEqual(qualification["agent_skill"]["sha256"], remediation["agent_skill"]["sha256"])
        self.assertEqual(
            qualification["agent_skill"]["sha256"],
            hashlib.sha256(qualification["agent_skill"]["instructions"].encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
