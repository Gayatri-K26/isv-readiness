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

## Step 4 - Multi-Domain Solution and Responsibility Model

### Objective

Represent an ISV's complete, versioned solution and shared-responsibility
boundaries so the agent can distinguish adapter work from product gaps,
external dependencies, evidence collection, rationalized skips, and unresolved
scope decisions.

### Evidence

- The AI Cloud Ready POR permits partial stacks to be qualified and documented,
  but requires an integrated IaaS, CaaS, and PaaS stack for full end-to-end
  validation.
- Current public BCM documentation states that BCM streamlines cluster
  provisioning, workload management, and infrastructure monitoring, supports
  Kubernetes orchestration, and documents Slurm integration.
- Current public Mission Control documentation and 2.3.0 release notes describe
  BCM as a component and identify autonomous job recovery, autonomous hardware
  recovery, Grafana dashboards, LaunchPad, and Kubernetes-delivered artifacts.
- Public product overviews do not establish that BCM or Mission Control alone
  supplies a complete tenant cloud control plane, SDN/VPC service, tenant IAM,
  tenant image registry, or every security control in the validation suite.
- Roadmap and preview claims were deliberately excluded from reference-profile
  coverage.

### Decisions

1. Keep solution qualification state separate from flat `gaps.json` rows.
2. Model actors, versioned components, dependency edges, NSRG layers, and source
   references as a validated acyclic solution graph.
3. Separate capability coverage (`covered`, `gap`, `out_of_scope`, `unknown`)
   from validation mode (`test`, `evidence`, `skip`, `deferred`).
4. Track both the capability owner and provider-adapter owner. They can differ,
   which prevents the agent from treating every external layer as ISV-editable.
5. Resolve optional step/category/class selectors deterministically. Equally
   specific overlapping selectors are errors rather than arbitrary choices.
6. Route resolved responsibilities to explicit actions:
   `implement_or_fix_adapter`, `request_external_adapter`, `collect_evidence`,
   `record_product_gap`, `skip_with_rationale`, or `request_scope_decision`.
7. Ship BCM and NVIDIA Mission Control profiles as draft qualification
   baselines. Their unresolved integrated-cloud domains and capability slices
   remain visible blockers, not inferred product deficiencies or false passes.

### Changed Files

- `schemas/solution-profile.schema.json`
- `src/isv_readiness/solution_profile.py`
- `examples/profiles/bcm.reference.yaml`
- `examples/profiles/nvidia-mission-control.reference.yaml`
- `tests/test_solution_profile.py`
- `SLOP.md`

### Verification

- Focused tests cover both reference profiles, cross-reference and dependency
  validation, domain aliases, capability overrides, all major action routes,
  readiness blockers, and ambiguous selectors.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 28 tests passed.
- `python3 -m compileall -q src tests`: passed.
- `python3 -m json.tool schemas/solution-profile.schema.json`: passed.
- `git diff --check`: passed.

## Step 5a - Profile-Aware Scanning

### Objective

Apply the solution responsibility model to deterministic gap rows without
changing `ai-cloud-validation` outcomes.

### Decisions

1. Keep scan status and evidence unchanged; add responsibility routing under
   `enrichment.solution_profile`.
2. Permit profile routing to disable `auto_fixable`, but never use it to turn a
   non-fixable row into an automatic edit.
3. Resolve current validation category metadata before domain defaults so
   capability slices such as Kubernetes CSI inherit the correct owner.
4. Allow `gapctl scan --profile` to derive covered, testable domains when
   `--domains` is omitted.
5. Add `gapctl profile` to validate profiles and expose qualification blockers
   before provider scripts are generated or executed.

### Changed Files

- `src/isv_readiness/scan/profile.py`
- `src/isv_readiness/cli.py`
- `tests/test_profile_enrichment.py`
- `tests/fixtures/ai-cloud-validation/isvctl/configs/suites/k8s.yaml`
- `SLOP.md`

### Verification

- Focused tests cover domain derivation, profile summaries, K8s capability
  overrides, report-schema compatibility, and out-of-scope edit suppression.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 32 tests passed.
- `python3 -m compileall -q src tests`: passed.
- `git diff --check`: passed.

## Step 5b - Cross-Domain Onboarding and Plan Export

### Objective

Cover both brand-new providers and existing configurations without duplicating
the upstream scaffold or executing tests before the merged plan is understood.

### Decisions

1. Delegate broad provider creation to the supported
   `isvctl provider scaffold` command; do not copy or reinterpret its template
   inside `isv-readiness`.
2. Treat selected domains as the readiness work scope. The upstream command may
   create the complete template, while the agent only asks for and scans inputs
   relevant to selected/profile-covered domains.
3. Preserve the lightweight `--domain k8s` flow for existing users. In the
   cross-domain flow, preserve K8s scripts created by the upstream scaffold and
   add only the missing top-level wrapper and ownership scope.
4. Prefer a checkout's existing `.venv/bin/isvctl`; fall back to
   `uv run isvctl` when no local executable exists.
5. Export the authoritative merged dry-run and catalog as a versioned,
   schema-valid `validation-plan.json` via `gapctl plan`.

### Changed Files

- `src/isv_readiness/onboarding.py`
- `src/isv_readiness/scan/k8s_onboard.py`
- `src/isv_readiness/validation_adapter.py`
- `src/isv_readiness/cli.py`
- `schemas/validation-plan.schema.json`
- `tests/test_onboarding.py`
- `tests/test_validation_adapter.py`
- `SLOP.md`

### Verification

- A real `gapctl plan` run against the current K3s provider produced a
  schema-valid plan with config/catalog version `0.8.0`, 2 steps, 42 validation
  entries, and 0 malformed entries. The 2 catalog-unknown entries are retained
  ReFrame adapter checks.
- Focused tests cover scaffold delegation, profile-derived intake questions,
  K8s script preservation, backward-compatible K8s onboarding, local executable
  selection, CLI plan export, and plan-schema validation.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 37 tests passed.
- `python3 -m compileall -q src tests`: passed.
- `python3 -m json.tool schemas/validation-plan.schema.json`: passed.
- `git diff --check`: passed.
