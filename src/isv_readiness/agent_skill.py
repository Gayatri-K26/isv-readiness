from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

SKILL_NAME = "isv-readiness-agent"
SKILL_VERSION = "0.1.0"
SkillPhase = Literal["qualification", "remediation"]


def skill_root() -> Path:
    return Path(__file__).with_name("skills") / SKILL_NAME


def with_agent_skill(request: dict[str, Any], phase: SkillPhase) -> dict[str, Any]:
    """Attach the repository-pinned reasoning workflow to a generator request."""
    root = skill_root()
    skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
    phase_text = (root / "references" / f"{phase}.md").read_text(encoding="utf-8")
    instructions = f"{skill_text.rstrip()}\n\n{phase_text.rstrip()}\n"
    enriched = dict(request)
    enriched["agent_skill"] = {
        "name": SKILL_NAME,
        "version": SKILL_VERSION,
        "phase": phase,
        "sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "instructions": instructions,
    }
    return enriched
