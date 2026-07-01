# ISV Readiness Engineering Log

This file records implementation decisions, assumptions, evidence, tradeoffs,
verification, and follow-up work for each shippable step. It is an engineering
decision log, not a transcript of private model reasoning.

## Working Assumptions

- "BMC" in the implementation request means NVIDIA Base Command Manager (BCM).
  If it instead means Baseboard Management Controller, the reference profile
  will be corrected without changing the solution model.
- Publication remains deferred. The current boundary ends with a reproducible
  technical qualification or validation bundle.
- Kubernetes remains the first executable vertical slice, but shared contracts
  and orchestration must support all `ai-cloud-validation` platforms.
- `ai-cloud-validation` is the source of truth for configuration validity and
  test outcomes. `isv-readiness` consumes its contracts and never edits suites.
- Provider and product changes remain human-gated. Generated fixes are proposed
  as patches or pull requests and are never auto-merged.

## Step 1 - Contract and Scope Audit

### Objective

Establish the compatibility and solution-level foundations required before
adding more agent behavior.

### Evidence

- The installed/current `ai-cloud-validation` catalog is version `0.8.0` and
  contains 174 entries across bare metal, control plane, IAM, image registry,
  Kubernetes, network, observability, security, Slurm, and VM platforms.
- The catalog currently contains 35 Kubernetes entries.
- Scanning the current K3s provider with the existing static scanner produced
  only setup and teardown rows. It silently omitted the 35 Kubernetes checks
  because the scanner only handled an older grouped-validation YAML shape.
- `isvctl test run --dry-run` emits a merged, schema-validated JSON config.
- `isvctl catalog list --json` emits a versioned test catalog.
- Terminal JUnit entries encode structured skip/error reasons in the XML
  element `type` attribute.

### Decisions

1. Add a version-aware adapter around supported `isvctl` machine interfaces.
2. Normalize every supported validation configuration shape into one internal
   test-plan representation.
3. Preserve unknown tests and metadata instead of silently dropping them.
4. Model a complete solution as a graph of versioned components and capability
   responsibilities; do not reduce ownership to one provider-wide boolean.
5. Keep `gaps.json` as row-level deterministic truth and add separate
   solution-profile and validation-plan contracts for journey state.

### Planned Commits

1. Engineering decision log.
2. Version-aware validation adapter and normalized plan.
3. Scanner integration with contract-drift regression coverage.
4. Multi-domain solution profile, ownership model, and BCM/NMC examples.
5. CLI/docs integration and end-to-end verification.

### Verification

- Both repositories were clean before implementation began.
- Existing tests and formatting checks will be run after every behavioral step.

