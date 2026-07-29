# Qualification workflow

1. Enumerate every declared domain and its pinned step and validation-class
   checks. Treat a class-only selector as matching every occurrence; constrain
   by step when the evidence does not support them all.
2. Map capabilities explicitly declared by authoritative ISV interfaces or
   documentation to the closest checks. An API specification is useful but not
   required; architecture documents, product documentation, and command
   references may establish a capability.
3. Match required behavior, not nearby terminology. Read or inventory evidence
   does not establish create, update, placement, retention, aggregation, policy,
   or other undeclared semantics. Support every grouped check independently.
4. Treat an API specification as evidence of declared interfaces, not proof
   that they work in the target environment.
5. Use `covered/test` when declared behavior maps to a check, even when the
   target version, credentials, hardware, topology, or runtime behavior remains
   unverified. Runtime validation determines whether it passes.
6. Use a covered domain default only when the evidence supports every pinned
   check. For each selected check, reconcile its minimum prerequisites with
   verified inventory: resource count, per-resource capacity, placement,
   accelerators per node, network or fabric, exact identities, and required
   runtime or scheduler features. Do not inherit scaffold defaults. Treat a
   missing lab prerequisite as a blocker or SME decision, not automatically as
   a product exclusion.
7. Use `out_of_scope/skip` only for an evidenced product-scope exclusion, never
   for a lab limitation. Otherwise use `unknown/deferred` for SME review. Use
   `gap` only when authoritative ISV evidence shows a required capability is
   absent.
8. Treat missing provider scripts as remediation gaps, not product capability
   gaps.
9. Cite supporting evidence in every domain rationale. Declare every cited
   evidence ID as a source.
10. Model the project provider as the ISV actor and default capability and
    provider-adapter ownership to it for owned domains. Add another actor only
    when evidence names it. Ownership remains an SME-reviewed suggestion.
11. Copy environment facts verbatim and do not assign numeric NSRG layers
    without an explicit source mapping.
12. Recheck that the draft contains every declared domain, no added scope, no
    credential values, and no inferred facts.
