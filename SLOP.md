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

## Step 2 - Version-Aware Validation Adapter

### Objective

Create a stable internal validation plan without coupling `isv-readiness` to
private Python APIs or one revision of the upstream YAML layout.

### Decisions

1. Use the supported `isvctl catalog list --json` and
   `isvctl test run --dry-run --no-upload` interfaces as the compatibility
   boundary.
2. Keep subprocess execution in a thin, injectable adapter and implement the
   normalization logic as pure functions.
3. Normalize grouped check maps, grouped check lists, direct check maps, and
   repeated list entries into the same plan contract.
4. Preserve unknown checks, malformed entries, extra group metadata, raw
   parameters, version information, and deterministic fingerprints. Contract
   drift must become visible evidence instead of silently reducing coverage.
5. Represent non-pytest execution categories such as ReFrame explicitly so a
   later executor can route them without changing the plan schema.

### Changed Files

- `src/isv_readiness/validation_adapter.py`
- `tests/test_validation_adapter.py`
- `SLOP.md`

### Verification

- Focused adapter tests cover current upstream YAML shapes, variants, unknown
  checks, malformed input, command construction, and CLI failures.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 21 tests passed.
- `python3 -m compileall -q`: passed for the adapter and its tests.
- `git diff --check`: passed.
- `uv run ruff check` is currently blocked before lint execution because the
  sibling `ai-cloud-validation` editable source is resolved as one setuptools
  package with three top-level projects. This packaging/tooling issue is kept
  visible for the CLI integration step; no dependency metadata was changed as
  part of the adapter commit.

## Step 3 - Scanner Contract-Drift Integration

### Objective

Make static coverage independent of a single historical validation YAML shape
and prove the scanner sees the current upstream Kubernetes suite.

### Decisions

1. Build scanner checks from the normalized validation plan instead of parsing
   only group-level `step` plus mapping-style `checks` entries.
2. Mirror the supported `isvctl` merge behavior for offline static scans:
   mappings merge recursively and later lists replace earlier lists. The CLI
   adapter remains the authoritative merged-config boundary for execution.
3. Represent validations with no lifecycle-step binding as `<validation>`
   coverage rows. They are declared correctly; their pass/fail outcome belongs
   to the dynamic `ai-cloud-validation` run, not provider-script discovery.
4. Emit malformed validation contracts as explicit `error` rows with
   `semantic_mismatch` classification instead of dropping them.
5. Store category, phase, provider-step requirement, and execution-adapter
   metadata in row enrichment without copying validation parameters or secrets.
6. Preserve existing gap IDs. Only repeated instances with the same class and
   step receive an additional deterministic identity component.

### Changed Files

- `src/isv_readiness/scan/scanner.py`
- `tests/test_scan.py`
- `tests/fixtures/ai-cloud-validation/isvctl/configs/suites/k8s.yaml`
- `SLOP.md`

### Verification

- Focused scanner tests cover grouped maps, repeated lists, grouped lists,
  direct maps, ReFrame routing, validation-only rows, malformed contracts, and
  duplicate-instance IDs.
- A static scan of the current upstream K3s provider now emits 44 unique rows:
  42 declared validations and 2 lifecycle rows, with no contract errors. The
  previous scanner emitted only the 2 lifecycle rows.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 23 tests passed.
- `python3 -m compileall -q src tests`: passed.
- `git diff --check`: passed.
