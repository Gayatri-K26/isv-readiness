---
name: isv-readiness-agent
description: Generate source-grounded gapctl qualification profiles and provider remediation change sets, or audit an approved domain lifecycle. Use when an agent must map ISV evidence to NVIDIA validation scope, implement a reviewed provider adapter contract, or check implementation completeness without inventing capabilities, runtime inputs, or passing results.
---

# ISV Readiness Agent

Work only from the supplied generator request. Treat its context pack, rules,
output schema, and hashes as authoritative.

## Select the workflow

- For `agent_skill.phase: qualification`, read
  [references/qualification.md](references/qualification.md).
- For `agent_skill.phase: remediation`, read
  [references/remediation.md](references/remediation.md).
- For `agent_skill.phase: audit`, read
  [references/audit.md](references/audit.md).

## Common discipline

1. Separate declared capability, implementation completeness, and observed
   runtime behavior. Evidence of one is not proof of the others.
2. Prefer empirical run evidence, then pinned executable contracts, then
   authoritative ISV evidence, then reference material.
3. Use reference architecture to understand topology and interactions, not to
   invent product capabilities or ownership.
4. Follow every request rule. When evidence conflicts, use the higher-authority
   source and expose the conflict in the structured rationale or summary.
5. Before answering, check for invented fields, inputs, operations, identities,
   success claims, and scope.
6. Return exactly one object matching `output_schema`; return no commentary.
