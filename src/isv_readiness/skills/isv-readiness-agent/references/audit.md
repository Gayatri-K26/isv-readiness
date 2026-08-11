# Domain completeness audit

1. Audit the approved capability scope against the supplied provider implementation. Do not edit files or
   propose a change set. Account for every supplied capability exactly once.
2. Treat the reviewed coverage, validation mode, ownership, and rationale as premises. Do not broaden scope.
   Use `scope_question` only when the supplied sources genuinely conflict about whether the provider owns a
   lifecycle effect; missing live credentials or an unavailable lab is not a scope conflict.
3. Derive expected lifecycle effects from the reviewed capability, its cited evidence, the domain contract,
   provider API references, and configured setup/test/teardown flow. Keep effects provider-neutral in the
   audit record, while citing concrete provider operations where the evidence supplies them.
4. Judge behavior, not file presence, command names, comments, output shape, or exit status. An inventory-only
   setup does not implement a reviewed create/provision effect, and a successful no-op teardown does not
   implement deletion of a resource owned by the run.
5. Trace each implemented effect to a supplied provider file and a concise code-level explanation. Do not
   claim an effect from a filename, TODO comment, placeholder, generic scaffold, or unexecuted example.
6. For a gap, choose the narrowest existing provider-owned lifecycle script or selected domain configuration
   that should be the primary remediation target. Never target validation-suite code or an unrelated domain.
7. Preserve resource lineage across setup outputs, tests, and teardown. Cleanup must be scoped to resources
   proven to be owned by the run, treat documented already-absent outcomes as success, and continue independent
   cleanup actions before reporting aggregated errors.
8. Report `implemented` only when the supplied code evidence covers every expected effect for that capability.
   Otherwise report `gap` with the missing effect and its implementation evidence left absent.
