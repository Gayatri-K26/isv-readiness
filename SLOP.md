# ISV Readiness Engineering Log

This file records implementation decisions, assumptions, evidence, tradeoffs,
verification, and follow-up work for each shippable step. It is an engineering
decision log, not a transcript of private model reasoning.

## Working Assumptions

- The user confirmed that "BMC" in the implementation request means NVIDIA
  Base Command Manager (BCM), not Baseboard Management Controller.
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
  part of the adapter commit. It was resolved in Step 5d by using the supported
  external CLI boundary instead of an umbrella editable package dependency.

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

## Step 5c - Cross-Domain Dynamic Ingestion

### Objective

Extend correctness-stage evidence beyond Kubernetes while retaining the
specialized K8s ownership classifier.

### Decisions

1. Execute or ingest exactly one domain per dynamic invocation so each JUnit
   artifact has an unambiguous provider config and rerun command.
2. Keep K8s setup-inventory and layer-aware classification specialized; route
   every other domain through a generic JUnit contract.
3. Join dynamic cases to static rows to recover provider step and validation
   category context without importing private validation internals.
4. Preserve testcase names and structured JUnit reason codes such as
   `step_not_configured` and `template_render_failed` in row enrichment.
5. Classify generic failures conservatively. Profile routing still decides
   whether the owning party permits an adapter edit, external handoff, evidence
   collection, or product-gap ticket.
6. Convert malformed or missing JUnit into an explicit `lab_env` error row
   rather than aborting report generation.

### Changed Files

- `src/isv_readiness/scan/dynamic.py`
- `src/isv_readiness/scan/k8s_dynamic.py`
- `src/isv_readiness/cli.py`
- `tests/test_dynamic.py`
- `tests/test_k8s_dynamic.py`
- `tests/fixtures/vm-dynamic/`
- `SLOP.md`

### Verification

- VM fixture tests cover pass, failure, missing-step skip, template-render
  error, log excerpts, static context joins, malformed XML, CLI merge, and gap
  schema validation.
- Existing K8s dynamic tests remain green and now assert structured reason
  preservation.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: 40 tests passed.
- `python3 -m compileall -q src tests`: passed.
- `git diff --check`: passed.

## Step 5d - Documentation, Packaging, and End-to-End Verification

### Objective

Make the implemented qualify-to-validate flow reproducible from the CLI and
remove development setup that contradicted the supported integration boundary.

### Decisions

1. Document the complete agentic architecture, deterministic contracts,
   guardrails, stage gates, action routes, and current-versus-next status.
2. Remove the direct editable dependency on the `ai-cloud-validation` umbrella
   project. `isv-readiness` shells out to a sibling checkout or an independently
   installed `isvctl`; it does not import the multi-project workspace as one
   package.
3. Commit the compact `uv.lock` for the actual runtime dependencies.
4. Permit `gapctl plan` to omit `--validation-root` when `isvctl` is already on
   `PATH`.
5. Exclude provider fixtures from Ruff because they model externally owned code;
   lint all project-owned source and tests.

### Changed Files

- `README.md`
- `docs/architecture.md`
- `pyproject.toml`
- `uv.lock`
- `src/isv_readiness/cli.py`
- Lint-only formatting in project-owned source/tests
- `SLOP.md`

### End-to-End Evidence

- `uv sync` resolves 8 packages and builds `isv-readiness` without installing
  the validation workspace or GPU dependencies.
- Installed `gapctl` help exposes scan, report, profile, plan, onboard, and the
  explicitly reserved fix/loop commands.
- A real K3s plan export remains schema-valid with 42 validations and no
  malformed entries.
- A real profile-aware K3s scan emits 44 unique rows: 39 pass and 5
  not-implemented. The BCM profile routes 38 rows to adapter work and 6 CSI,
  identity, or network checks to `request_scope_decision`.
- The real plan and gap artifacts both validate against their committed schemas.

### Verification

- `uv run python -m unittest discover -s tests -v`: 41 tests passed.
- `uvx ruff check src tests`: passed.
- `python3 -m compileall -q src tests`: passed.
- All three JSON schemas pass `python3 -m json.tool`.
- `uv lock --check`: passed.
- `git diff --check`: passed.

## Step 6 - Simulation Boundary and Upstream Compatibility

### Objective

Distinguish framework integration evidence from product qualification and prove
that an upstream validation-suite change remains visible without allowing
`gapctl` to modify `ai-cloud-validation`.

### Evidence

- The DSX Air environment used for the live exercise is a limited-access
  Kubernetes simulation running on VMs. It is useful for CLI, JUnit, log, and
  report plumbing but is not authoritative evidence of BCM, Mission Control, or
  provider ownership.
- The VM exercise used `ISVCTL_DEMO_MODE=1`, dummy resource identifiers, and
  documentation addresses. It validated generic non-Kubernetes ingestion but
  did not validate a real VM lifecycle.
- Scope questions are not answered by an agent. Their text and required inputs
  live in the versioned profiles; deterministic selectors map scan rows to those
  capabilities, and `_resolve_action` maps approved coverage/mode/ownership to
  an action. Kubernetes validation-to-layer prefixes remain explicit code rules.

### Decisions

1. Treat DSX Air results as integration/plumbing evidence, not product
   qualification or a basis for assigning product ownership.
2. Keep BCM and Mission Control profiles draft until a real SME supplies the
   shared-responsibility facts. Limited lab access is a `lab_env` constraint,
   not proof that a capability is out of product scope.
3. Preserve the real non-Kubernetes run as a future qualification gate; the
   demo run satisfies only the parser/orchestration smoke test.
4. Simulate upstream drift in a copied fixture by adding a previously unknown
   validation. Require a changed config fingerprint, one additional normalized
   plan entry, one additional scan row, schema-valid outputs, deterministic
   domain-default routing, and byte-for-byte preservation of the changed suite
   after scanning.
5. Treat Git persistence separately: upstream changes persist through commits
   in `ai-cloud-validation`; the compatibility test proves consumption and
   non-mutation, not version-control durability.

### Changed Files

- `tests/test_upstream_compatibility.py`
- `SLOP.md`

### Verification

- The mock `K8sFutureUpstreamCheck` changes the normalized config fingerprint,
  is retained as catalog-unknown and valid, appears as one new static row, and
  falls back to `kubernetes.default` routing.
- Both validation-plan and gap-report schemas accept the changed outputs.
- The copied upstream suite is unchanged by plan/scanner consumption.

## Step 7 - Guarded Candidate-to-Patch Proposal

### Objective

Create the first code-change boundary without granting a model, candidate file,
or gap report permission to edit provider source directly.

### Decisions

1. Make candidate generation a replaceable seam. `gapctl fix` accepts a
   candidate replacement file produced by a human, model, or external tool;
   model-provider integration is not required to test the safety boundary.
2. Require all three independent authorizations before emitting a patch:
   unresolved status is fixable, profile action is
   `implement_or_fix_adapter`, and scanner remediation is `auto_fixable` with a
   target.
3. Resolve the target against the explicitly selected provider root and allow
   only its `scripts/` subtree. Reject path traversal, absolute escapes,
   symlink targets, and config/suite/engine paths even if an input report claims
   they are editable.
4. Reject non-UTF-8 or oversized candidates, common private-key/access-key and
   literal-credential patterns, and invalid Python, JSON, or YAML syntax.
5. Emit a standard unified diff and SHA-256 identifier. Never write the target,
   apply the patch, run infrastructure, create a branch, or open a pull request.
6. Keep targeted verification separate. A later verifier can consume the patch
   after human review without weakening the proposal gate.

### Changed Files

- `src/isv_readiness/fixes.py`
- `src/isv_readiness/cli.py`
- `tests/test_fixes.py`
- `README.md`
- `docs/architecture.md`
- `SLOP.md`

### Verification

- Focused tests cover successful existing-file diff generation, source
  non-mutation, CLI output, unresolved scope, path traversal, non-script
  targets, secret-looking literals, and invalid Python syntax.
- The emitted patch names only the provider-relative approved target.

## Step 8 - Deterministic Loop-State Controller

### Objective

Persist one-gap-at-a-time selection and stop conditions without turning report
strings into executable commands or bypassing human patch review.

### Decisions

1. Drive the controller from successive immutable `gaps.json` reports rather
   than invoking scans or shell commands internally.
2. Select blocker routes before fixable work. Unresolved scope, evidence,
   external-adapter, and product-gap routes stop the loop before code
   generation; an approved `skip_with_rationale` is a resolved disposition.
3. Permit `ready` only when the selected row routes to
   `implement_or_fix_adapter` and the scanner independently authorizes a target.
4. Record attempts only through an explicit `--attempted-gap` matching the
   previously selected ID. Merely re-reading a report never consumes retry
   budget.
5. Stop after the configured per-gap retry budget while retaining deterministic
   report fingerprints and the last 100 state transitions.
6. Define `ready`, `blocked`, and `complete` in a versioned loop-state schema.
   The current controller is the safe state core for a future autonomous
   runner; it is not itself an automatic patch/apply/rescan engine.

### Changed Files

- `src/isv_readiness/loop.py`
- `src/isv_readiness/cli.py`
- `schemas/loop-state.schema.json`
- `tests/test_loop.py`
- `README.md`
- `docs/architecture.md`
- `SLOP.md`

### Verification

- Tests cover blocker-first selection, explicit retry accounting, retry
  exhaustion, approved-skip completion, mismatched attempt rejection, CLI state
  persistence, reload, history, and schema validation.
- macOS temporary-path assertions now compare resolved paths, removing the
  `/var` versus `/private/var` false failures without changing product code.
- `uv run python -m unittest discover -s tests -v`: 52 tests passed.
- `uvx ruff check src tests`: passed.

## Step 9 - Agent Instructions, Targeted Verification, and Controlled Application

### Objective

Turn a guarded patch proposal into a verifiable and explicitly authorized
provider-script change without giving reports, candidates, or loop state direct
write authority.

### Decisions

1. Add a repository-level `AGENTS.md` so future agents inherit the source-of-
   truth boundary, DSX simulation classification, ownership rules, path limits,
   verification commands, documentation requirements, and no-push/no-apply
   defaults.
2. Verify only static gaps in this slice. Dynamic gaps require a real reviewed
   `isvctl` rerun and are rejected rather than being misrepresented as verified
   by syntax or offline analysis.
3. Copy the selected provider into an isolated temporary workspace, including a
   sibling top-level K8s wrapper when present. Install the candidate only in that
   copy and run the deterministic static scanner against the selected domain.
4. Require the selected row to move to `pass` and reject newly introduced
   unresolved rows or regressions of prior passing rows. Do not mutate the real
   provider during verification.
5. Bind the verification manifest to the gap ID, domain, target, patch hash,
   candidate hash, and pre-application target hash. Persist the before/after
   selected status and explicit regression list in a versioned schema.
6. Require `gapctl apply --apply` as explicit write authorization. Rebuild the
   guarded proposal at application time and reject changed source, changed
   candidate, changed patch, unsuccessful verification, or regressions.
7. Back up an existing target before using an fsync-backed temporary file and
   atomic `os.replace`. Preserve the target mode for replacements and candidate
   mode for new files.
8. Keep rollback execution, dynamic verification, branch creation, commits,
   pushes, and pull requests outside this slice. The application result records
   the backup and hashes needed for later audited rollback tooling.

### Changed Files

- `AGENTS.md`
- `src/isv_readiness/verification.py`
- `src/isv_readiness/cli.py`
- `schemas/verification-manifest.schema.json`
- `schemas/application-result.schema.json`
- `tests/test_verification.py`
- `README.md`
- `docs/architecture.md`
- `SLOP.md`

### Verification

- Focused tests cover isolated success, unresolved-candidate failure, source
  non-mutation, manifest and application schemas, atomic replacement, backup
  preservation, source/candidate drift rejection, explicit-flag refusal, and a
  complete CLI verify/apply flow.
- `gapctl verify` and `gapctl apply` help expose only explicit file and
  verification inputs; neither consumes a remediation command from the report.
- `uv run python -m unittest discover -s tests`: 58 tests passed.
- `uvx ruff check src tests`: passed.
- Both new JSON schemas, Python compilation, the lockfile, and
  `git diff --check` passed.

## Step 10 - Workspace Bootstrap and Agent Context Boundary

### Objective

Give an ISV one reproducible entry point that can adopt or clone
`ai-cloud-validation`, pin the exact upstream commit, define the ISV-owned
assessment scope, and assemble useful implementation context without exposing
credentials or allowing secondary sources to redefine validation.

### Brain-Food Investigation

- The public validation repository describes itself as a provider-agnostic test
  framework that maps high-level requirements to provider stubs. Its public
  issues also contain validation backlog and requirement discussions that can
  clarify intent but may be newer, older, or less authoritative than the
  installed checkout.
- The public NSRG describes capability layers and shared infrastructure
  responsibilities without prescribing a product implementation. It is useful
  for qualification and scope questions, not as an executable pass/fail oracle.
- Available NVIDIA program material distinguishes selected-scope qualification
  from integrated full-stack validation. Partial solutions can be assessed and
  documented, but selected green rows must not be presented as complete
  metal-to-model validation.
- MCP availability belongs to the agent host, not this Python package. A local
  CLI cannot assume a Confluence connector, authentication model, or server
  name. A normalized import boundary preserves portability while allowing an
  authorized host agent to contribute selected evidence.

### Decisions

1. Use a local agentic CLI/workflow engine as the product boundary. Keep scope,
   selection, verification, and execution deterministic; reserve a frontier
   model for bounded candidate synthesis.
2. Add `isv-project.yaml` as the engagement root of trust. Record repository
   URL/ref and resolved commit, provider discovery state, assessment mode,
   selected domains, API/spec references, context declarations, and execution
   policy.
3. Store only credential environment-variable names. Reject assignment-shaped
   values during bootstrap and never serialize values when reporting which
   inputs are available.
4. Make bootstrap dry-run by default. `--write` may clone a missing checkout but
   never pulls, switches, or rewrites an existing checkout.
5. Make all HTTP and GitHub synchronization opt-in through `--allow-network`.
   Use `GITHUB_TOKEN` only in request headers and persist only filtered issue
   fields.
6. Normalize local files, API specs, public docs, issues, and MCP exports into a
   redacted cache. Reject oversized inputs and exclude common secret files from
   tree ingestion.
7. Build one context pack per selected gap. Prefer executable contracts and API
   specifications, limit excerpts by relevance and character budget, and mark
   issues/MCP exports advisory.
8. Default Kubernetes ownership to unknown. Require literal booleans for
   answered layers and reject null/coerced/unknown keys so unanswered intake is
   never silently converted to out-of-scope.

### Changed Files

- `schemas/project.schema.json`
- `schemas/context-pack.schema.json`
- `src/isv_readiness/project.py`
- `src/isv_readiness/context.py`
- `src/isv_readiness/scan/k8s_onboard.py`
- `src/isv_readiness/scan/k8s_scope.py`
- `src/isv_readiness/cli.py`
- `tests/test_project.py`
- `tests/test_context.py`
- `tests/test_k8s_onboard.py`
- `tests/test_k8s_scope.py`
- `README.md`
- `docs/architecture.md`
- `AGENTS.md`
- `SLOP.md`

### Verification

- Focused tests cover dry-run bootstrap, existing checkout adoption, clone
  planning, exact commit pinning, provider discovery, project schema validity,
  credential-name validation, network opt-in, GitHub PR filtering, token
  non-persistence, redaction, MCP import, relevance filtering, context-pack
  schema validity, and unknown Kubernetes ownership.
- `uv run python -m unittest discover -s tests`: 66 tests passed.
- `uvx ruff check src tests`, Python compilation, all JSON schemas,
  `uv lock --check`, and `git diff --check`: passed.

## Step 11 - Generator Contract and Transactional Multi-file Changes

### Objective

Fill the agentic generation seam without coupling the workflow to one model
vendor or giving a model direct filesystem authority, while supporting the
small multi-file edits real provider onboarding requires.

### Decisions

1. Use an explicit command adapter as the portable model boundary. Send one
   JSON request over stdin, require one JSON object on stdout, invoke without a
   shell, enforce a timeout, and pass only explicitly allowlisted environment
   variables in addition to minimal process variables.
2. Bind generator output to both the selected gap and canonical context-pack
   SHA-256. Reject Markdown fences, logs mixed with output, wrong gap IDs,
   context drift, duplicate targets, bad content hashes, and oversized sets.
3. Keep the model's output declarative. It may request `create` or `replace`,
   but the deterministic guard resolves every path, validates syntax/secrets,
   checks operation preconditions, and emits the actual patch.
4. Allow at most 12 files and 2 MB. Permit provider `scripts/`, only the config
   filename corresponding to the selected domain, and only the exact selected
   provider wrapper for Kubernetes. Require the scanner-selected remediation
   target to be included so helper/config edits cannot replace the primary fix.
5. Preserve the earlier single-file commands for compatibility while making
   `generate`, `change-propose`, `change-verify`, and `change-apply` the complete
   multi-file path.
6. Verify the whole set in an isolated provider copy and reject a selected gap
   that does not pass or any newly introduced static regression.
7. Treat application as a transaction: re-derive the proposal, compare all
   source/content/patch hashes, stage every file on its destination filesystem,
   create durable per-transaction backups, then replace. Restore replaced files
   and remove newly created files if a later replacement or hash check fails.
8. Keep live infrastructure execution outside this stage. Static success makes
   a change eligible for a reviewed targeted run; it is not dynamic proof.

### Changed Files

- `schemas/change-set.schema.json`
- `schemas/change-proposal.schema.json`
- `schemas/change-verification.schema.json`
- `schemas/change-application.schema.json`
- `src/isv_readiness/generation.py`
- `src/isv_readiness/changes.py`
- `src/isv_readiness/change_verification.py`
- `src/isv_readiness/fixes.py`
- `src/isv_readiness/verification.py`
- `src/isv_readiness/cli.py`
- `tests/test_generation.py`
- `tests/test_changes.py`
- `tests/test_change_verification.py`
- `README.md`
- `docs/architecture.md`
- `AGENTS.md`
- `SLOP.md`

### Verification

- Tests cover strict generator I/O, environment isolation, context/gap binding,
  content hashes, duplicate targets, combined patches, selected-domain config,
  Kubernetes wrapper limits, required primary target, secret rejection,
  isolated multi-file success, source non-mutation, schema validation,
  transactional backups, application, and drift rejection.
- `uv run python -m unittest discover -s tests`: 74 tests passed.
- `uvx ruff check src tests`, Python compilation, all JSON schemas, and
  `git diff --check`: passed.

## Step 12 - Live Feedback, Review-gated Agent Runner, and Evidence Bundle

### Objective

Connect the implemented contracts into a usable domain workflow that can
scaffold an unstarted provider, close eligible gaps, learn from real validation
artifacts, restore failed attempts, and produce a reproducible review bundle
without weakening scope, review, credential, or infrastructure gates.

### Upstream Contract Investigation

- Current `isvctl test run` supports config files, lifecycle phases, repeated
  labels, a JUnit output path, `--no-upload`, and extra pytest arguments after
  `--`.
- A gap row carries the installed validation class but not necessarily all
  upstream labels. Targeted verification therefore uses the validated class as
  a safe pytest `-k` selection. `isvctl` continues to execute configured setup,
  test, and teardown phases; selection does not bypass lifecycle management.
- JUnit is the authoritative feedback contract. Process exit status alone is
  insufficient because missing, skipped, or malformed result rows must remain
  visible to the deterministic classifier.

### Decisions

1. Add both a project policy (`execution.allow_live_runs`) and an invocation
   gate (`--run-live`). Require both for infrastructure execution.
2. Verify the checkout still matches the manifest's pinned commit immediately
   before a live run. Construct the command from supported `isvctl` options,
   never from a report's remediation string.
3. Build a minimal child environment from declared credential and runtime
   variable names. Inject a non-secret API endpoint only through its declared
   `base_url_env`. Reject missing required credentials before executing.
4. Redact captured command output before writing it. Preserve JUnit, the
   redacted log path, the exact command arguments, exit status, normalized gap
   report, and selected statuses in a versioned live-run result.
5. Generate a draft qualification profile when bootstrap domains are explicitly
   declared ISV-owned. Keep it draft and record its assumption. Require an
   explicit reviewed profile for `full_validation`; never synthesize one.
6. Within the adapter-work route, select an authorized/fixable row before a
   non-editable row. Non-code scope/evidence/product routes still take priority,
   and an uneditable adapter gap remains a blocker after eligible work is done.
7. Let dynamic gaps pass isolated *static safety* verification when no static
   regression is introduced, but require the subsequent live result to resolve
   the dynamic gap. Static verification never claims dynamic success.
8. Persist `agent-run` state by project/profile identity and domain. Advance
   through `awaiting_generator`, `awaiting_review`, and `awaiting_live`; exact
   patch-hash approval authorizes only one transaction.
9. On targeted live failure, use the application record to restore replaced
   files or remove created files. Refuse rollback if applied files or backups
   drifted. Feed redacted verifier evidence into a later context pack only when
   deterministic routing still authorizes an edit.
10. Require a final whole-domain live pass after static selected scope is green.
    Report `complete` only after both conditions hold.
11. Build a sanitized evidence directory containing project/profile, agent
    state, reports, proposals/patches, verification, application/rollback, and
    live result JSON. Inventory provider files by hash without copying source.
    Exclude raw contexts, API specs, MCP exports, generated source payloads,
    credential/process environments, backups, and raw logs.

### Changed Files

- `schemas/live-run.schema.json`
- `schemas/agent-state.schema.json`
- `schemas/change-rollback.schema.json`
- `schemas/bundle-manifest.schema.json`
- `schemas/project.schema.json`
- `src/isv_readiness/live.py`
- `src/isv_readiness/agent.py`
- `src/isv_readiness/bundle.py`
- `src/isv_readiness/change_verification.py`
- `src/isv_readiness/context.py`
- `src/isv_readiness/project.py`
- `src/isv_readiness/loop.py`
- `src/isv_readiness/cli.py`
- `tests/test_live.py`
- `tests/test_agent.py`
- `tests/test_bundle.py`
- `tests/test_change_verification.py`
- `tests/test_loop.py`
- `tests/test_project.py`
- `README.md`
- `docs/architecture.md`
- `AGENTS.md`
- `SLOP.md`

### Verification

- Tests cover both live-run gates, checkout drift, missing credentials, minimal
  environment injection, API URL injection, targeted selection, redaction,
  JUnit ingestion, generated qualification scope, full-validation profile
  refusal, fixable-row ordering, agent review/hash gates, targeted and final
  live completion, explicit rollback, rollback drift, bundle exclusions,
  provider inventory, and bundle schemas.
- `uv run python -m unittest discover -s tests`: 83 tests passed.
- `uvx ruff check src tests`, Python compilation, all JSON schemas,
  `uv lock --check`, and `git diff --check`: passed.

## Step 13 - Concrete Codex Generator Adapter

### Objective

Ship one usable model adapter without coupling the workflow contracts to Codex
or granting a generation subprocess repository write authority.

### Evidence and Decisions

1. The installed Codex CLI exposes non-interactive stdin plus
   `--output-schema`, `--output-last-message`, `--ephemeral`,
   `--ignore-user-config`, and `--sandbox read-only`.
2. Add `gapctl-codex-generator` as a reference implementation of the existing
   command-adapter contract. It writes the request's output schema into an empty
   temporary directory, sends the complete redacted request over stdin, and
   reads only the schema-constrained final-message file.
3. Run Codex ephemerally, ignore user configuration, skip the Git-repository
   requirement, and select a read-only sandbox. The adapter therefore has no
   provider checkout in its working directory and cannot directly implement or
   apply its proposal.
4. Keep model selection optional. Codex authentication remains owned by the
   installed CLI; the adapter does not read, accept, or serialize an API key.
5. Emit one compact JSON object to stdout. All Codex event/progress output stays
   inside the adapter process, and the existing generator/change-set guards
   independently revalidate the result.

### Changed Files

- `src/isv_readiness/codex_generator.py`
- `tests/test_codex_generator.py`
- `pyproject.toml`
- `README.md`
- `docs/architecture.md`
- `SLOP.md`

### Verification

- Tests cover Codex executable selection, ephemeral/user-config/read-only
  flags, stdin request delivery, output-schema/final-message paths, optional
  model selection, nonzero exit handling, missing schema, and missing output.
- `uv run python -m unittest discover -s tests`: 85 tests passed.
- `uvx ruff check src tests`, Python compilation, all JSON schemas,
  `uv lock --check`, `git diff --check`, and the installed adapter `--help`:
  passed.
