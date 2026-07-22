# ISV Readiness Engineering Log

This file records implementation decisions, assumptions, evidence, tradeoffs,
verification, and follow-up work for each shippable step. It is an engineering
decision log, not a transcript of private model reasoning.

## Working Assumptions

- The user confirmed that "BMC" in the implementation request means NVIDIA
  Base Command Manager (BCM), not Baseboard Management Controller.
- Publication is explicit and consumes only canonical successful run evidence;
  no separate bundle or archive is required by the Lab Service.
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

## Step 14 - Full-validation Semantic Gate

### Objective

Prevent an explicit but draft, partial, or qualification-stage profile from
turning a selected-scope run into a nominal full-validation engagement.

### Decisions

1. Validate a supplied profile before cloning or writing workspace state.
2. Require every selected project domain to resolve in that profile.
3. For `full_validation`, require `profile_status` to be `reviewed` or
   `confirmed`, journey stage `validate`, component coverage across NSRG layers
   1-4, and no declared required-domain or required-capability blockers.
4. Keep qualification bootstrap unchanged: explicit ISV-owned domains may
   generate a draft profile, and its output remains labeled qualification.

### Changed Files

- `src/isv_readiness/project.py`
- `tests/test_project.py`
- `README.md`
- `docs/architecture.md`
- `SLOP.md`

### Verification

- Tests cover missing full-validation profiles and rejection of a supplied
  draft qualification profile before workspace creation.
- Full verification remains green: 85 tests, Ruff, compilation, every JSON
  schema, lockfile, and diff checks passed.

## Step 15 - Qualify/Validate Phases, Removing the Full-Stack Mode

### Objective

Align the tool's vocabulary with NVIDIA's AI Cloud Ready program, where an ISV
is qualified and then validated for the scope it owns. Confirmed against program
sources (AI Cloud Ready Initiative POR; NCX docs): the initiative runs three
stages, Qualify -> Validate -> Publish, where Qualify is commercial/technical
assessment and scoping and Validate is end-to-end testing against the NSRG (NCP
Software Reference Guide). "Full validation across NSRG layers 1-4" is a property
of an integrated, multi-owner stack, not something a single ISV performs. The
prior `qualification` vs `full_validation` *mode* conflated phase (assess vs
test) with scope breadth (one layer vs all), and the layer-1-4 gate was
unreachable and misleading for a single-layer ISV.

### Decisions

1. Remove the `AssessmentMode` type, the `--assessment-mode` flag, the
   `assessment.mode` project field, and the Step 14 full-stack layer-1-4 gate.
2. Treat qualify and validate as *phases*, tracked by the existing
   `journey.stage` enum (`qualify` then `validate`). Qualify = bootstrap +
   profile/SME (assess and scope owned domains, no execution). Validate =
   plan/onboard/scan/generate/verify/apply/loop/live/bundle over the owned scope.
2b. The static gap `scan` is deliberately part of Validate (it requires a
   provider implementation and begins "does the software do what it says").
3. Replace the domain `required_for_full_validation` field with `owned`. An ISV
   may own one or many domains; readiness is assessed only over owned domains.
   Domains owned by other actors are external dependencies and never block the
   ISV's own validation.
4. Rename `SolutionProfile.qualification_summary()` -> `scope_summary()`,
   reporting `owned_domains` and `validation_ready` over the owned scope.
5. The evidence bundle outcome is `validation_complete` | `incomplete`; the
   `assessment_mode` field is removed. Publish and integrated multi-owner
   roll-up remain out of scope.
6. Reframe CLI help and docs into the two phases; no new umbrella commands.

### Changed Files

- `src/isv_readiness/project.py`, `solution_profile.py`, `bundle.py`,
  `context.py`, `cli.py`
- `schemas/project.schema.json`, `solution-profile.schema.json`,
  `bundle-manifest.schema.json`, `context-pack.schema.json`
- `examples/profiles/bcm.reference.yaml`,
  `examples/profiles/nvidia-mission-control.reference.yaml`
  (each now marks the 4 ISV-owned domains `owned: true` and 6 partner-owned
  external dependencies `owned: false`)
- `tests/test_project.py`, `test_solution_profile.py`, `test_bundle.py`,
  `test_profile_enrichment.py`
- `README.md`, `docs/architecture.md`, `SLOP.md`

### Verification

- Full verification green: 85 tests, Ruff, compilation, every JSON schema,
  lockfile, and diff checks passed.

### Follow-up: drop the stale domain->NSRG-layer guess

With the layer-1-4 gate removed, nothing in the code reads `component.nsrg_layers`
any more; it is now purely descriptive metadata. The bootstrap draft's
`layer_by_domain` heuristic also predated the confirmed NSRG model
(bare-metal=1 -> VM=2 -> Kubernetes=3 -> AI Platform=4, with
network/hardware-lifecycle/attestation cross-cutting) and mis-assigned several
domains (e.g. `bare_metal: [2]`). Rather than ship a wrong auto-guess, the draft
no longer emits `nsrg_layers`; assigning NSRG layers is now explicit SME work
during qualify. `nsrg_layers` is dropped from the component `required` list in
the schema and parsed with an empty-tuple default; hand-authored reference
profiles keep their layer assignments. Files: `project.py` (remove
`layer_by_domain`, update draft + assumption note), `solution_profile.py`
(`_parse_component` default), `schemas/solution-profile.schema.json`. 85 tests,
Ruff, schema, lockfile, and diff checks green.

## Step 16 - Hardening from the First Real End-to-End Engagement

A full simulated ISV engagement (provider "acme": scaffold -> auto generation
with a blind Claude-CLI adapter -> review-gated apply -> eight live `isvctl`
runs against a real GPU host) stress-tested the pipeline and exposed four
behavioral defects, all fixed here with regression tests.

### Decisions and fixes

- `gapctl auto --apply` is now apply-ONLY (`_apply_reviewed_scratch`): it
  consumes the previously staged scratch exactly as reviewed and never
  regenerates. Rationale: model output is not byte-deterministic, so a
  regenerating apply could never match the reviewed hash - it burned a full
  generation pass and then refused. Apply with no staged scratch is an explicit
  error; a hash mismatch is a refusal that reports the staged hash.
- A malformed generation (or guard violation) inside the `auto` loop counts as
  a failed attempt for that gap and the loop continues; it no longer aborts the
  whole run. An exhausted gap parks instead of halting the loop, so one bad gap
  cannot starve the rest of the domain.
- Live-run success no longer treats `skipped` rows as failures
  (`live.py`): a skipped row is a declared config exclusion (label opt-outs);
  `isvctl` itself exits 0 for such runs. Success now requires exit 0, at least
  one executed `pass`, and no fail/error/not_implemented among selected rows.
  Observed live: a fully green 16-pass/20-skip run was reported as failed and
  re-parked at `awaiting_live` indefinitely.
- Parked-gap reasons distinguish "retry budget exhausted" from "scanner did not
  authorize an edit" (e.g. an unwired step needing a config/scope decision),
  quoting the scanner evidence. The previous wording claimed a human route was
  required even when the route WAS implement_or_fix_adapter.

### Evidence

- Live engagement ladder (one failure class per run): infra outage; three
  cross-script contract bugs (container-name convention, missing `vpc_id`,
  missing Name/CreatedBy tags); empty templated `key_file` silently skipping
  downstream steps; a 60 s step timeout from per-poll SSH round-trips; a
  container VM-ism (`/proc/uptime` never resets - use `.State.StartedAt`);
  and the skipped-as-failure predicate above.

### Follow-up

- `agent-run` prints `awaiting_live` both before a live run and after a failed
  one; the terminal status is ambiguous without reading history. Consider a
  distinct `live_failed` surface status.
- A step that dies without stdout produces no fail row (checks skip as
  `step_no_output`), so the loop can read a failed orchestration as green;
  the gap model should represent step-level no-output failures.

## Step 17 - Dead-Field Pruning and Patterns-Only Reference Boundary

### Decisions

- Removed `gap_type` from the gap model, schema, scanners, K8s classifier,
  report renderer, and context deserializer. An audit showed no routing,
  guardrail, loop, or auto logic ever branched on it; decisions come from the
  profile action, `status`, and `remediation.auto_fixable`. The human-facing
  "why" survives in `classification_note` enrichment and evidence messages.
  (It did flow to the generator inside the context pack's embedded row, so its
  removal marginally changes generator input - accepted for simplicity.)
- Removed the `isv_context` report envelope. `repo_access`/`creds_scope` were
  write-only constants; `run_env`/`api_spec` were copied verbatim from
  `isv-project.yaml`, which remains the authoritative record of engagement
  context. No code, renderer, or context pack read any of it.
- Stopped embedding the AWS reference implementation's full text in the
  context pack. `remediation.aws_reference` remains a path pointer on the
  embedded gap row, but generated code must derive from the ISV's own API
  spec and the executable contract - reference providers are patterns only.
- Removed the `AwsReferenceStep` synthetic coverage checks. Only the suite's
  own validation contracts define required steps; the AWS provider wiring an
  extra step no longer produces a gap row for another provider. The per-step
  AWS pairing that fills the `aws_reference` pointer is unchanged.

### Rationale

- Fields no decision logic consults can silently drift from reality (the
  `semantic_mismatch` overload) and no test catches it; either wire a field
  to behavior or delete it.
- Full-text reference embedding risked derivative generated code and exceeded
  the recorded "patterns only" trust boundary for reference providers.

## Step 18 - Autonomy Stance After Qualify

### Decision

After the qualify stage, human review concentrates at two enforced gates:
hash-bound patch application and policy-gated live execution. The context
pack is the generator's complete, self-sufficient input - inspecting it is a
debugging aid, never a workflow gate. `auto` is the primary validate-phase
flow; the decomposed context-pack/generate commands remain for debugging and
adapter development. (No code change - the gates were already implemented
this way; README prose updated to match.)

## Step 19 - HTML Context Sources Cache Prose, Guidance Must Earn Its Budget

### Decision

- `context-sync` extracts visible text from fetched HTML pages (stdlib
  HTMLParser; script/style/svg skipped) before redaction and caching. The
  default NSRG page is a rendered docs app: 471k chars of markup carrying
  7.6k chars of prose. The whole extracted document now fits in a pack
  untruncated - "give the model the entire thing" at the information level,
  not the markup level.
- `_cached_items` skips non-authoritative excerpts whose relevance score
  is zero. Previously a zero-signal source contributed its first 12k chars
  (pure boilerplate for the NSRG page) to every pack - a quarter of the
  default budget spent on nothing. Authoritative sources (the API spec)
  are still always included.

## Step 20 - Claude Code Reference Generator Adapter

### Decision

Added `gapctl-claude-generator`, a second reference adapter for the generator
command contract, driven by the first real engagement (BCM-as-ISV on a Krusty
COD cluster) on a machine without the Codex CLI. Differences from the Codex
adapter, both forced by Claude Code's surface:

- No server-side constrained decoding: the adapter validates the candidate
  against `request.output_schema` locally (Draft 2020-12) and retries once,
  feeding the validator errors and the truncated previous response back.
- No `--sandbox read-only`: isolation is `-p` print mode with
  `--disallowedTools "*"`, `--max-turns 1`, and an empty temporary cwd - the
  request on stdin is the model's entire world.

The harness-side guarantees (env allowlist, no shell, one-JSON-object stdout,
gap-id and context-pack hash binding) are unchanged and vendor-agnostic - the
adapter stays dumb, generation.py stays the enforcement point.

### Amendment (first real generation run)

The request rules asked the generator to compute each change's
content_sha256 - impossible for a language model, which can only invent a
plausible hash; the harness integrity check correctly rejected the first
real change set. Both adapters now compute the per-file hash over the
model's content themselves (the hash is transport integrity from adapter
to harness, re-verified at propose/apply - not model proof-of-work), and
the Claude prompt tells the model to emit 64 zeros as a placeholder.
Also noted: on macOS the Claude CLI's Keychain lookup needs USER/LOGNAME,
which the harness env allowlist strips - pass
`--generator-env USER --generator-env LOGNAME`.

### Amendment (macOS Keychain authentication)

The manual generator-environment workaround was the wrong product boundary for
the simplified workflow: `gapctl validate --generator claude` could report
`Not logged in` even while `claude auth status` showed an active SSO session.
The generator process allowlist now includes only the non-secret `USER`
identity in addition to the existing minimal process variables. A direct
minimal-environment reproduction confirmed that `USER` is sufficient for the
Claude CLI to recover its Keychain-backed session; no credential value is
forwarded, persisted, or added to model context.

## Step 21 - Simplified Context Sources, Empirical Runs Outrank Declared Claims

### Decision

Trimmed the context-source model to what an ISV can actually reach and made
runtime evidence first-class, driven by the qualify-drafting design review:

- **Removed `mcp_export` entirely** (default `nvidia_ai_cloud_ready` source,
  schema kind, `context-import` command, `import_context_source`). The ISV
  runtime never has NVIDIA MCP access; a source only the builder can populate
  violated the two-environment boundary and earned its complexity nowhere.
- **Network sync is no longer opt-in.** `context-sync --allow-network` is
  gone; declared network sources (API spec URL, NSRG, GitHub issues) are
  always fetched, and an unreachable optional source degrades to a `missing`
  record instead of a `deferred` gate. The prior flag mostly produced stale
  caches in practice.
- **New `empirical` trust tier above `authoritative`.** `scan --run` now
  records each execution under `.gapctl/runs/<run-id>/` (`junit.xml`,
  `isvctl.log`, `run.json`; timestamp-prefixed ids so lexicographic order is
  chronological). `build_context_pack` injects the latest run for the gap's
  domain ahead of every declared source. Rationale: an observed runtime
  result overrides what any spec or doc claims — this is the mechanism that
  will let an agent-drafted solution profile be empirically demoted
  (covered → gap) instead of trusted.

The remaining source set: ai-cloud-validation checkout + provider API spec
(authoritative), reference impls + NSRG (reference), GitHub issues
(advisory), recorded runs (empirical). Run logs are redacted on write like
live-run artifacts.

## Step 22 - Agent-Drafted Qualify Profiles, SME-Ratified

### Decision

Implemented the qualify-drafting pipeline on the Step 21 substrate, reusing
existing contracts instead of new machinery:

- **Catalog (mapping target).** `gapctl catalog` distills the pinned suites
  into per-domain checks/test-ids/steps via the existing
  `IsvctlAdapter.plan()` contract (`isvctl catalog list --json` +
  `test run --dry-run`) — upstream already joins catalog metadata to suite
  wiring; we only reshape. Suite filenames are an explicit 10-entry map
  (upstream mixes `bare_metal.yaml` with `control-plane.yaml`).
- **Qualify pack.** `build_qualify_pack` is profile-scoped (all declared
  domains at once): catalog entries (authoritative) + cached API spec/NSRG/
  issues + latest recorded run per domain (empirical, ranked first). Shares
  the per-gap pack's budget/redaction/trust machinery via extracted helpers
  (`_fit_budget`, generalized `_cached_items`/`_run_items`).
- **Draft.** `gapctl qualify-draft` sends the pack to any generator adapter
  with `solution-profile.schema.json` as `output_schema` (the adapters were
  already schema-generic; `generation.py` grew a shared `dispatch_generator`
  so it stays the single enforcement point). The draft is deterministically
  hardened, never trusted: `profile_status` forced to `draft`, journey forced
  to `qualify/in_progress`, drafted domain set must exactly equal declared
  scope (scope invention is an error, not a warning).
- **Ratify.** Manual and human, by design: `gapctl profile --draft` prints a
  per-domain diff plus empirical conflicts (drafted `covered` whose latest
  recorded run failed → exit 1). The SME edits the draft, moves it into
  place, and flips `profile_status` — the existing `unknown`-blocks-
  `validation_ready` gate does the rest.

Boundary kept: the model proposes the capability→domain mapping; it never
decides ownership or scope, and a failing run outranks any drafted claim.

## Step 23 - Authoritative Sources Enter Packs Whole

### Decision

Driven by the BCM dress rehearsal: the 12k per-source relevance excerpt was
silently trimming the ISV's own API guide — the one document the drafting
generator must see completely. Two changes:

- `_cached_items` no longer excerpts `authoritative` records; the pack budget
  (`_fit_budget`, trust-ordered) remains the only bound. Reference/advisory
  sources keep the 12k excerpt + must-match-scope rule.
- `qualify-draft`/`build_qualify_pack` default budget raised 48k → 120k: a
  qualify pack spans every declared domain (catalog alone ~40k for four
  domains) where the 48k default was sized for single-gap fix packs, which
  keep it.

Considered and rejected: removing the budget entirely ("assume big-context
models"). The adapter contract must stay portable to small/internal ISV
models, scarcity is what makes trust ordering a guarantee, and the validate
loop builds packs per gap per iteration where unbounded packs multiply cost.

### Amendment (Step 22, found during the BCM dress rehearsal)

`scope_summary` treated every non-`covered`/`test` row as blocking, which made
`out_of_scope`+`skip` capabilities block `validation_ready` forever — there was
no way to express "we own this domain, minus the corner this environment can
never provide." Extracted `_blocks_readiness`: `covered`+`test` passes,
`out_of_scope`+`skip` passes (a signed scope decision, not a deficiency),
everything else (gaps, unknowns, half-pairings like `out_of_scope`+`test`)
still blocks. The qualify-draft prompt teaches the model the same distinction.

### Amendment (Step 22, second dress-rehearsal find): Slurm wrapper onboarding

The first engagement's wrapper solution covered Kubernetes only; Slurm is the
same unified-suite pattern upstream (suites/slurm.yaml carries its own
commands, scaffold ships scripts/slurm/ but no config), so onboarding a slurm
domain left one honest-but-noisy scan error: "No provider config found for
domain 'slurm'". `execute_provider_onboarding` now writes
`config/slurm.yaml` (imports the suite, points commands at the provider's own
scripts — the k3s.yaml pattern) when the domain is selected and the file is
absent; hand-authored configs are preserved on rerun. No scanner changes:
config/<domain>.yaml is already a resolution candidate.

### Amendment (Step 22, third dress-rehearsal find): agent may wire missing steps

The scanner marked "Provider config does not wire a command for this required
step" rows non-auto-fixable, parking them for a human even though (a) the
wiring lives in the selected domain's own config — already inside the guarded
fix surface `_authorize_provider_path` permits — and (b) profile routing
already answers the authority question (implement_or_fix_adapter only fires on
SME-signed covered+test ISV-owned rows). Product decision (GK): the pipeline
exists to do the ISV's clerical work; the human gate belongs at patch review,
not at YAML wiring. `auto_fixable` is now true for unwired-step rows with the
domain config as the primary target — `_require_primary_target` forces the
generated change set to actually wire the step. All other parking (masked
failures, scope decisions, external adapters) is unchanged.

Also found live: seven bare_metal steps (governance/health/IB/sanitization)
are demanded by the suite but wired in NO provider — not even the azure
reference. Filed as upstream feedback; locally the profile routes the
IB/GPU-sanitization ones out_of_scope and the agent now wires the rest.

Plus an honesty fix in auto's review gate: parked auto-fixable gaps now
distinguish "not attempted within this run's iteration budget" and "N failed
attempts, retry budget remains" from the previously blanket (and usually
false) "attempts exhausted".

## Step 24 - Installed-CLI and High-Level Workflow Hardening

### Decisions

- Moved all runtime JSON schemas under `src/isv_readiness/schemas/`, declared
  them as setuptools package data, and routed schema reads through one guarded
  loader. A built wheel was inspected and installed into a clean Python 3.12
  environment to prove that schema validation no longer depends on an
  `isv-readiness` source checkout.
- Removed the second clone implementation from the high-level `init` wrapper.
  Bootstrap now owns validation, clone, and commit pinning, and the requested
  validation ref is recorded correctly. Invalid input is rejected before any
  clone begins.
- Reused the live runner's normalized report in `gapctl test` instead of
  rescanning and re-parsing JUnit. Each high-level test now writes a canonical
  `.gapctl/runs/<run-id>/` empirical record, while `gaps.json` retains the full
  declared project scope.
- Made `gapctl status` operate consistently on report dictionaries, refresh
  legacy partial-scope reports, and require passing recorded live evidence for
  every owned domain before claiming bundle readiness.
- Hardened publication before any remote mutation: validate the bundle schema,
  refuse incomplete bundles, validate local JUnit presence, use the exact
  upstream platform enum for every domain, require an explicit platform for a
  mixed-platform bundle, and normalize endpoint URLs.

### Rationale

The high-level commands were added without regression tests and passed the
lower-level suite while crashing on ordinary status calls, losing multi-domain
scope, and mislabeling several portal result types. The package also omitted
every runtime schema even though the product is installed as a standalone
wheel. These are boundary and evidence-integrity failures, not presentation
issues, so the fixes keep one authoritative implementation per operation and
add command-level coverage.

## Step 25 - Simplify Gap Decisions and Qualification Gates

### Decisions

- Added one deterministic gap decision with only four outputs: `blocking`,
  `edit_eligible`, `action`, and `reason`. Loop selection, auto parking, change
  authorization, live success, status, and bundle readiness now consume that
  policy instead of maintaining subtly different status lists.
- Kept raw scan/JUnit status unchanged. A skip resolves only through an
  explicit `skip_with_rationale`; `step_not_configured` remains a raw JUnit
  skip but is eligible for a guarded config candidate when ownership and
  profile gates allow it.
- Required a `reviewed` or `confirmed` profile in the `validate` journey stage
  before generation, live execution, agent turns, or evidence bundling. Draft
  profiles remain useful for scanning and qualification review, but carry no
  edit authority.
- Preserved upstream `test_id` and labels in gap rows and dynamic matches.
  `min_req` is a deterministic tie-breaker within the same route. At this step,
  the unused nullable milestone remained only for schema `0.1.0` compatibility
  and had no decision role.
- Replaced substring stub detection with syntax-aware Python inspection and
  executable-line shell inspection. TODO comments no longer create false
  gaps, while invalid Python becomes an explicit fixable coverage error.

### Rationale

The previous behavior answered “is this open?”, “may a model edit it?”, and
“is validation complete?” differently in different commands. That allowed a
draft profile to authorize edits, treated all skips as resolved in some paths,
and stranded a fixable dynamic missing-step row. The simpler contract is: keep
the observed result, require an explicit reviewed route, and use one function
for every downstream decision. No new profile fields or inferred milestones
were introduced.

## Step 26 - Remove the Unsupported Gap Milestone

### Decisions

- Removed the milestone field from `GapRow`, static and dynamic construction,
  context deserialization, the sample report, and all tests.
- Bumped only the gap-report contract to schema `0.2.0` and made the already
  emitted upstream-label list required. Other artifact schemas remain at their
  existing versions because their shapes did not change.
- Removed the unused milestone slot from the deterministic gap-ID spine.
  Existing workspaces must regenerate `gaps.json`, which is already the normal
  static-scan/status behavior.

### Rationale

The pinned upstream validation contract provides test IDs and labels but no
milestone. Keeping a permanently null field or inventing a value adds a false
concept and makes downstream consumers guess what it means. A narrow,
versioned gap-schema change is simpler and more accurate than a compatibility
shim for data that never existed.

## Step 27 - Publish Canonical Runs Directly

### Decisions

- Removed the `bundle` command, implementation module, manifest schema, and
  agent-state requirement. The Lab Service never received the bundle; publish
  read a small manifest and uploaded JUnit separately, so the extra artifact
  was a local gate with a second, incompatible notion of completion.
- Added one readiness assessment over the reviewed profile, complete current
  `gaps.json`, and latest canonical `.gapctl/runs/` record for every owned
  domain. Both `status` and `publish` consume it. JUnit must exist and be
  well-formed, the latest run must exit zero and contain a dynamic pass, and no
  shared-policy blocker may remain.
- `publish` now discovers `isv-project.yaml` like the other high-level
  commands, verifies the validation checkout still matches its pinned commit,
  and creates one correctly typed Lab Service test run per owned domain. JUnit,
  platform, tags, and run identity come from the canonical local record instead
  of operator-supplied duplicate flags.
- `agent-run` remains an advanced per-gap orchestration option, but it is no
  longer a submission prerequisite. The installed journey is now `init` ->
  profile review -> `fill` -> `test` -> `status` -> `publish`.

### Rationale

The evidence already existed in the canonical run directory, and the portal
API consumes JUnit rather than an archive. Requiring a second command forced
new ISVs to understand agent work directories, duplicated readiness logic, and
made the simple wrappers dead-end before publication. Direct publication keeps
the safety checks while deleting the unused transport format and the mixed-
platform ambiguity.

## Step 28 - Make the Four-Command Journey the Only CLI

### Decisions

- Replaced the 1,100-line public parser with four commands only: `init`,
  `qualify`, `validate`, and `publish`. The former bootstrap, context, profile,
  scan, generation, change, agent, live, report, and status commands are no
  longer an alternative operator interface. Their deterministic modules remain
  internal and directly unit-tested.
- Made `init` own context synchronization in addition to clone/pin, catalog,
  and provider scaffolding. Required API-spec failures stop initialization;
  unavailable optional references remain visible without blocking it.
- Added one resumable `qualify` orchestration. It builds the evidence-grounded
  proposal, keeps unresolved ownership visible, displays a content hash, and
  promotes only an explicitly approved proposal. The command performs the
  status/stage transition, so the ISV does not edit workflow-control YAML.
- Added one resumable `validate` orchestration across every owned domain. It
  stages and verifies provider changes in scratch copies, resumes an existing
  patch without regeneration, obtains hash-bound approval, applies it, rescans,
  stops on parked static blockers, asks once for real-cloud authorization, runs
  domains individually, and prints the shared readiness result.
- Kept the internal live policy gate but satisfy it with a transient authorized
  project after the explicit `validate` confirmation. This removes the manual
  `allow_live_runs` YAML toggle without weakening authorization.
- Fixed multi-domain evidence replacement: each new domain run now combines
  current full-scope static rows with prior dynamic rows from other domains and
  the current domain's new dynamic rows. A later run no longer erases an
  earlier domain's passing evidence.
- Replaced advanced CLI tests with direct service tests and added public-surface
  tests proving only the four journey commands are exposed, qualification
  promotion is human-gated, validation keeps patch/live approval inside one
  command, and multi-domain dynamic evidence persists.

### Rationale

The lower-level commands accurately exposed implementation stages but forced a
new ISV to learn the tool's internals and manually connect them. Hiding them in
documentation would still leave two product interfaces and duplicate choices.
The simpler boundary is one command per user intent while retaining the same
deterministic checks underneath: initialize the engagement, approve what is in
scope, validate it safely, and publish canonical evidence.

## Step 29 - Keep Qualification Context Complete and Capability-Focused

### Decisions

- Replaced the single-page NSRG import with a bounded collection import driven
  by NVIDIA's published NCP `llms.txt` index. Every Markdown page under the
  Software Reference Guide, Part 1, and Part 2 navigation families is fetched
  and cached as one required reference source; failure to fetch any page stops
  context import instead of presenting a partial guide as complete.
- Qualification now preserves reference material whole in addition to suite
  catalogs and authoritative ISV evidence. Its guarded budget is 300k
  characters and fails closed if a source would be omitted or truncated.
  Per-gap validate packs retain relevance excerpts because they answer one
  implementation question rather than an entire ownership assessment.
- Removed GitHub issues as a context-source type. They no longer add broad,
  unstable noise to qualification. Pack construction filters cached records to
  current manifest sources, and cache schema `0.2.0` forces existing workspaces
  to refresh once into the new complete-guide format.
- Clarified qualify semantics: match ISV-declared capabilities to the closest
  applicable pinned checks; treat an API specification as authoritative for
  declared interfaces but not runtime success; use `covered/test` to mean a
  capability should be tested; and leave missing provider implementations for
  the validate-phase scanner. Domain defaults remain preferred, with capability
  entries only for genuine exceptions.
- Made the Codex adapter translate the full product JSON Schema into Codex's
  supported Structured Outputs subset while retaining the original schema for
  final product validation. The adapter also finds macOS Codex.app without a
  PATH workaround and reports the actual error tail instead of echoing the
  beginning of a large prompt.

### Rationale

The BCM rehearsal showed that the introduction page alone omitted most of the
reference architecture, while broad issue matching selected unrelated catalog
and storage discussions. It also exposed a semantic ambiguity: a declared API
capability is enough to route an upstream test, but only a live run proves that
the implementation works. Complete stable references plus explicit mapping
rules are both simpler and more accurate than attempting to recover missing
context with relevance-ranked issue snippets.

## Step 30 - Default Partial Qualification Scope Conservatively

### Decisions

- A `covered/test` domain default is now permitted in generator instructions
  only when supplied ISV evidence explicitly maps every check in the pinned
  domain catalog.
- Partial products use grouped `covered/test` capability entries for explicitly
  mapped checks. They may use `out_of_scope/skip` as the unmatched default only
  when the product evidence explicitly excludes every unmatched check;
  otherwise the default remains `unknown/deferred` for SME resolution.
- Scope exclusion is not inferred from missing lab hardware, credentials, or
  runtime evidence. Provider script TODOs remain validate-phase implementation
  gaps rather than qualification gaps.
- Capability matching is semantic: an inventory/read surface cannot stand in
  for undeclared placement, retention, policy, or mutation behavior, and every
  check in a grouped selector needs its own support in the cited evidence.
- Catalog mappings use both step and validation class. A class-only selector
  matches every lifecycle occurrence of that class and is allowed only when the
  evidence supports all of them.
- Unverified runtime behavior no longer justifies `unknown/deferred` when the
  supplied contract explicitly declares the capability. Numeric `nsrg_layers`
  are omitted unless a source supplies the exact numbering.
- The mapping rules are defined once and shared by the qualification pack and
  the generator request so the two instruction surfaces cannot drift.

### Rationale

The first complete-context BCM draft marked bare-metal, Kubernetes, and
observability as covered by default while naming only nine exceptions. That
silently treated unmatched hardware-ingestion, governance, network telemetry,
storage telemetry, and fabric checks as BCM capabilities. Conservative defaults
make partial ISV scope explicit without creating one profile row per upstream
check.

## Step 31 - Align Generator Timeouts and Clean Up Process Trees

### Decisions

- Raised the shared generator-adapter timeout from 300 to 900 seconds while
  retaining the built-in Codex and Claude model timeout at 600 seconds. The
  adapter must finish or report its own timeout before its caller expires.
- Centralized captured subprocess execution for the shared boundary and both
  built-in adapters. On timeout, POSIX runs terminate and reap the complete
  subprocess group instead of killing only the immediate adapter and leaving a
  detached model process behind.
- Resolve built-in generator adapters beside the running `gapctl` executable
  before falling back to `PATH`, so a development environment cannot silently
  pair its CLI with an older globally installed adapter.
- Kept timeout policy internal rather than adding another ISV-facing option.
  A timeout is an execution failure, not a qualification or gap decision.

### Rationale

The first BCM `validate` rehearsal exposed a contradictory timeout stack: the
outer generator boundary stopped after 300 seconds while the Codex adapter
allowed its child 600 seconds. The parent exited during a valid long-running
generation and left `codex exec` orphaned. A longer outer envelope plus one
process-tree cleanup helper fixes the boundary without changing the four-command
journey or weakening review gates.

## Step 32 - Source-Grounded Provider Generation Guardrails

### Evidence

The second synthetic BCM rehearsal reached the review gate after a long
bare-metal generation pass. Its patch passed schema, syntax, path, and static
rescan checks but was not safe to apply: scripts introduced undeclared runtime
inputs, mixed verified and unverified TLS, emitted a raw serial-console excerpt,
used a UUID in setup while lifecycle paths assumed a hostname, and configured a
60-second runner around a 900-second internal deadline. The review also called
the patch "verified," which overstated what an isolated static rescan proves.

### Decisions

1. Add a compact provider-neutral authoring contract to every validate request:
   use only declared runtime names and source-backed interface behavior;
   preserve one configured resource identifier across lifecycle steps; keep TLS
   verification enabled; keep internal deadlines inside the step timeout; emit
   only required structured result fields; and treat edit-eligible checks
   sharing one target as one adapter contract.
2. Put the complete names-only runtime contract and related target gaps in the
   context pack. Add optional repeatable `gapctl init --input NAME` arguments so
   non-secret runtime requirements are declared during the existing init
   command rather than through another workflow. Represent related rows with
   only their validation contract fields, raise the per-gap bound to 120k
   characters, and fail closed rather than silently truncating or omitting a
   selected source.
3. Reject undeclared environment references in every changed candidate, common TLS
   verification bypasses, raw output/response fields in result mappings, and
   explicit Python deadlines longer than the configured provider step. Unchanged
   files remain outside the selected fix, but a generated replacement cannot
   activate or preserve unsafe demo-only scaffold behavior.
4. Return the exact guard or static-verification failure to the next model
   attempt. Count retries by remediation target, not gap row, so multiple
   validations backed by one script share one retry budget.
5. Require declared non-secret runtime inputs as well as credentials before a
   live run. Continue to pass only declared inputs, injected API endpoints, and
   the minimal process environment. An explicitly supplied empty environment
   remains empty instead of falling back to the parent process environment.
6. Label review output "statically verified candidate". Only a successful live
   NVIDIA validation run establishes provider behavior.

### Simplicity boundary

Do not add provider-name checks, BCM endpoint rules, or a universal identifier
heuristic to `gapctl`. A deterministic layer cannot know whether an arbitrary
provider accepts a hostname, UUID, URI, or numeric ID. The generic rule is to
preserve the identifier established by the authoritative contract and
configured step data flow; ambiguity must survive to model feedback and human
review instead of being guessed. This keeps the guardrails reusable for every
ISV while enforcing the safety properties code can actually prove.

### Verification

- Focused tests cover declared and undeclared inputs (including environment
  helper functions), insecure TLS, raw evidence fields, timeout mismatches,
  complete related-target context, fail-closed context budgets, exact retry
  feedback, target-shared retry budgets,
  required non-secret live inputs, and the generator rule set.
- `uv run python -m unittest discover -s tests`: 131 tests passed.
- Ruff, Python compilation, lock validation, and diff checks passed.

## Step 33 - Bind Generation to Exact Upstream Consumer Contracts

### Evidence

The stopped bare-metal BCM rehearsal produced candidates that were mechanically
valid but semantically wrong. One returned the BCM head address where the pinned
validation class opens direct SSH to the test instance, one translated BCM `UP`
to `up` while the suite expects `running`, and one left downstream-consumed
connection and resource fields as empty strings. The prior context described the
gap rows but did not include the code that consumes provider output. Ctrl+C also
left the adapter and nested Codex process running after the parent CLI exited.

### Decisions

1. Include the exact relevant pinned suite entries, step-output schemas, and
   validation class implementations in each selected-target context pack. Raise
   the fail-closed per-gap budget from 120k to 180k characters so this contract
   is not silently shortened.
2. Derive downstream-required output fields from the provider's own
   `steps.<step>.<field>` templates. Reject a Python candidate only when an
   emitted result mapping leaves one of those fields provably `None` or an empty
   string. Do not treat valid empty collections, un-emitted mappings, or unknown
   dynamic assignments as failures.
3. Permit a schema-valid empty change set with a non-empty summary. This is the
   generator's structured refusal when no source-grounded provider-owned edit
   can satisfy the pinned contract. Park the shared remediation target with the
   exact blocker instead of consuming retries or fabricating a pass.
4. Extend the captured-subprocess boundary to clean up on KeyboardInterrupt and
   SIGTERM as well as timeout. A terminated adapter immediately stops and reaps
   its nested model process before it exits.

### Simplicity boundary

Do not encode BCM state names, jump-host topology, or provider-specific API
rules in the scanner. Exact upstream code gives the model and reviewer the
semantic contract; deterministic checks remain limited to facts the syntax and
configured data flow can prove. A genuine interface mismatch becomes one parked
reason, not another profile type, command, or inference layer.

### Verification

- Focused tests cover exact upstream context, downstream-required empty outputs,
  dynamic and unused mappings, structured generator refusal in both workflows,
  and Ctrl+C process-group cleanup.
- `uv run python -m unittest discover -s tests`: 137 tests passed.
- Ruff, Python compilation, lock validation, and diff checks passed.

## Step 34 - Scope Adapter Decisions by Script or Configured Step

### Evidence

The next BCM bare-metal review staged one sound `host_status_log.py` candidate
but parked 68 rows. Seven edit-eligible unwired steps were collapsed into one
contract because their scanner remediation target was the shared
`config/bare_metal.yaml` file. The parked lifecycle-script reasons also claimed
that a script-target candidate could not change that configuration, although
the existing guarded multi-file boundary explicitly allows the selected domain
config and requires only that the primary remediation target be included.

### Decisions

1. Define one reusable adapter contract unit in `decision.py`. Script-backed
   rows group by script target because multiple validations genuinely consume
   one implementation. YAML-backed rows group by target and step name because
   a domain file contains many unrelated adapters.
2. Use that same unit for related-gap context, retry accounting, verifier
   feedback, structured generator blockers, and parking reasons. One failed or
   refused config step can no longer consume another step's model budget.
3. State the existing edit boundary directly in every generator request: a
   non-empty change set must include the selected remediation target, and may
   also include other provider-owned scripts plus the selected domain config
   when the same adapter contract requires coordinated changes.

### Simplicity boundary

This adds no command, schema, profile concept, or provider-specific rule. The
unit is derived from two facts already present on every gap row: remediation
target and step name. The distinction is structural rather than semantic:
scripts are adapter implementations; YAML files are containers for many step
definitions.

### Verification

- Focused tests cover shared-script grouping, same-step config grouping,
  different-step config isolation, retry isolation, context isolation,
  generator edit-boundary wording, and coordinated script/config timeout
  changes.
- `uv run python -m unittest discover -s tests`: 140 tests passed.
- Ruff, Python compilation, lock validation, and diff checks passed.

## Step 35 - Preserve Unrelated Domain Configuration

### Evidence

The next bare-metal rehearsal proposed one missing health step but also removed
the domain configuration's license text and comments and reformatted every
unrelated command argument. Static verification accepted the equivalent YAML
structure because it checked the merged configuration rather than the intended
edit scope. The health adapter also used a provider-native grouping label even
though the pinned contract described deployment primitives, showing that a
weak executable assertion must not replace the documented semantic contract.

### Decisions

1. For a replacement of an existing selected-domain YAML file, locate the
   scanner-selected step as a block-list item. Removing that step block from
   the original and candidate must leave the surrounding text unchanged.
2. Require the candidate to contain exactly one selected step. This permits a
   missing step to be added and an existing step's command, arguments, or
   timeout to change, while rejecting whole-file formatting, comment deletion,
   and edits to other steps.
3. Tell every provider generator to honor documented semantics as well as
   executable assertions. A provider-native concept may be relabeled as a
   contract primitive only when supplied evidence establishes the mapping.

### Simplicity boundary

Do not build a general YAML-preserving editor or provider-specific semantic
checker into `gapctl`. The deterministic rule compares text outside one step
block; the source-grounded model and human review handle meanings that cannot
be proven mechanically.

### Verification

- Focused tests cover a coordinated script/timeout change, insertion of one
  missing step, rejection of unrelated formatting and timeout changes, and the
  generic semantic-authoring rules.
- `uv run python -m unittest discover -s tests`: 141 tests passed.
- Ruff, Python compilation, lock validation, schema loading, and diff checks
  passed.

## Step 36 - Regenerate After Review Rejection

### Evidence

After the config-preservation fix was installed, rerunning `gapctl validate`
continued to display the earlier unsafe patch. The pending-review resume path
was behaving as designed, but declining the prompt left the review markers in
place, so the operator had no simple way to request a new proposal.

### Decisions

1. A `no` response at the patch review gate removes only
   `auto-review.json` and `auto-review.patch` for that domain.
2. Leave the real provider untouched. On the next invocation, the normal auto
   workflow recreates its scratch provider from the real provider before
   generation, so no rejected candidate content is reused.
3. Keep rejection and regeneration as two explicit operator actions. A
   rejection exits instead of immediately starting another potentially long
   model run.

### Verification

- The journey test builds a valid pending review, declines it, verifies that
  only the review markers are removed, and proves the generator is not invoked
  during rejection.
- `uv run python -m unittest discover -s tests`: 142 tests passed.
- Ruff, Python compilation, lock validation, schema loading, and diff checks
  passed.

## Step 37 - Preserve Input and Retry Semantics

### Evidence

The regenerated bare-metal review preserved unrelated YAML correctly, but its
host-log adapter passed a provider-derived managed-node string to nested SSH
without validating option-like values. Its health adapter interpreted absent
optional failure booleans as failures; the permissive upstream aggregation
check could still pass while reporting every healthy node as degraded. A
lifecycle gap also ended with the generic reason "attempts exhausted," hiding
the last deterministic rejection that would explain what to fix.

### Decisions

1. Require provider-derived subprocess arguments to be treated as untrusted.
   Validate them against a narrow source-backed syntax or use a supported
   end-of-options boundary; shell quoting alone is not option-injection safety.
2. Require optional provider response fields to retain their declared
   semantics. An absent optional boolean is not assigned a default unless the
   supplied contract defines one.
3. Carry the last per-contract-unit guardrail or static-verification feedback
   into the parked reason when retries remain or are exhausted.

### Simplicity boundary

Do not add provider-specific hostname rules or a general-purpose taint analyzer
to `gapctl`. These are generic authoring constraints; deterministic retry state
only preserves the exact failure it already knows.

### Verification

- Focused tests cover the new generator rules and exact exhausted-retry
  feedback shared across one adapter contract unit.
- `uv run python -m unittest discover -s tests`: 142 tests passed.
- Ruff, Python compilation, lock validation, schema loading, and diff checks
  passed.

## Step 38 - Keep Qualification and Runtime Uncertainty Separate

### Evidence

The next bare-metal run produced zero candidates and parked all 70 blocking
rows. The generator treated the reviewed BCM capability mappings as unproven,
rejected standard fail-closed implementation choices, and required every
response envelope, optional field default, SSH client behavior, and runner
timeout margin to be a formal provider guarantee. This repeated qualification
inside validation and converted ordinary runtime uncertainty into structural
impossibility.

### Decisions

1. Treat the reviewed solution-profile capability mapping and rationale as an
   approved implementation premise. `validate` implements that decision; it
   does not reopen scope because runtime behavior is untested.
2. Generate a bounded adapter for declared response shapes and fail explicitly
   on unsupported runtime data. Refuse only when a required provider interface
   is absent or the pinned validation contract is structurally incompatible
   with provider-owned edits.
3. Permit standard verified client behavior to realize an established access
   flow, including the current remote SSH user and host-side SSH configuration.
   This does not authorize invented credentials or a reachability claim.
4. Base results on required state fields and let explicit optional failure
   indicators override them. Missing optional data alone neither proves success
   nor makes implementation impossible.
5. Allow a configured runner timeout to include bounded orchestration headroom
   beyond a source-backed provider deadline without reclassifying that margin
   as a provider recovery threshold.

### Simplicity boundary

Keep one workflow boundary instead of adding confidence levels or another
profile state: qualification decides applicability, generated adapters fail
closed, and live validation proves behavior. Real direct-SSH versus jump-host
contract mismatches remain parked.

### Verification

- Focused generator tests require the reviewed-mapping premise, fail-closed
  runtime handling, narrow refusal boundary, standard-client allowance,
  optional-field precedence, and runner-headroom distinction.
- `uv run python -m unittest discover -s tests`: 142 tests passed.
- Ruff, Python compilation, lock validation, schema loading, and diff checks
  passed.

## Step 39 - Enforce Source-Backed Lifecycle Timing

### Evidence

A clean synthetic bare-metal rehearsal generated a reboot adapter with a
50-second internal recovery deadline and left the configured runner timeout at
60 seconds even though the authoritative ISV specification declared a
1,200-second lifecycle threshold. The existing timeout guard checked only that
the internal deadline fit inside the runner; it could not reject two mutually
consistent values that were both shorter than the supplied provider contract.

### Decisions

1. Normalize the optional
   `runtime.operation_timing.lifecycle_step_timeout_seconds` value only from
   authoritative API-spec context. Reject invalid or conflicting declarations
   instead of guessing.
2. State that normalized threshold explicitly in every generator adapter
   request. A model must update the selected lifecycle step rather than shorten
   a provider deadline to fit a scaffold default.
3. For a changed lifecycle adapter, reject a configured step timeout or an
   explicit internal recovery deadline below the normalized source threshold.
   Keep the existing rule that the internal deadline cannot exceed the runner.
4. Apply the floor only to lifecycle steps changed by the candidate. Unrelated
   incomplete scaffold steps do not block a safe change elsewhere in the
   domain.

### Simplicity boundary

This does not infer timing from prose, prescribe one universal bare-metal
timeout, or add a provider name check. Specifications without the optional
machine-readable value retain the existing generic envelope guard and human
review boundary.

### Verification

- The regression test recreates the rejected 50-second internal and 60-second
  runner candidate against a 1,200-second authoritative floor, then proves a
  1,200-second internal deadline with 1,260-second runner headroom is accepted.
- The actual stopped BCM reboot candidate is rejected by the new guard with
  the source-backed 1,200-second reason.
- `uv run python -m unittest discover -s tests`: 144 tests passed.
- Ruff, Python compilation, lock validation, schema parsing, and diff checks
  passed.

## Step 40 - Fail Fast and Reap Timed-Out Generator Sessions

### Evidence

The first Claude bare-metal rehearsal produced one statically verified adapter
that contradicted the supplied access topology, then spent repeated long model
calls without another verified change. The configured model deadline did not
bound the observed wall time; the prior `communicate()` timeout can exclude a
macOS sleep interval. Terminating the parent killed the adapter before it could
clean its separately-sessioned Claude child, leaving that process orphaned. The
generic auto loop also treated a nested model timeout like a malformed
candidate and repeated the identical request against the provider retry budget.

### Decisions

1. Bound captured subprocesses with a wall-clock deadline checked in short
   communication intervals, so a deadline is recognized promptly after macOS
   wakes instead of excluding the sleep interval.
2. On timeout, Ctrl+C, or SIGTERM, signal the adapter gracefully first so its
   own cleanup boundary can reap the nested model session. Escalate to SIGKILL
   after five seconds and never wait forever on pipes inherited by an orphan.
3. Give model timeouts a distinct adapter exit code and infrastructure-error
   type. Stop the validation run after the first timeout; do not classify it as
   provider evidence or repeat it using the candidate retry budget.
4. Make connection topology an explicit structural authoring rule. When
   provider evidence requires an intermediate hop and the pinned consumer has
   no compatible proxy input, the generator must return a structured refusal
   rather than substitute the hop or claim direct resource reachability.

### Simplicity boundary

Do not add provider names, BCM fields, or text heuristics that pretend every ISV
specification expresses topology the same way. Exact provider evidence and the
pinned consumer remain model and human-review inputs; deterministic code owns
only process cleanup and unambiguous infrastructure-failure classification.

### Verification

- Focused tests cover wall-clock expiry, SIGTERM-to-SIGKILL escalation, a real
  separately-sessioned nested child, distinct adapter timeout exits, one-call
  auto abort, and the topology authoring rule.
- `uv run python -m unittest discover -s tests`: 151 tests passed.
- Ruff, Python compilation, lock validation, schema parsing, and diff checks
  passed.

## Step 41 - Align Model and Adapter Time Budgets

### Evidence

The BCM rehearsal completed and statically staged one narrow adapter, then the
next Codex request reached the built-in 600-second model limit. That shared
`describe_instance.py` contract carried 167,873 characters of source-grounded
context, including every relevant pinned validation consumer, with no omitted
or truncated items. The outer adapter still allowed 900 seconds, but its extra
time could never help because the nested model process stopped first.

### Decisions

1. Centralize generator limits so the Codex adapter, Claude adapter, and outer
   dispatcher cannot silently drift apart again.
2. Allow Codex's single schema-constrained call 1,680 seconds. Allow each of
   Claude's two possible schema attempts 840 seconds. Keep the full
   source-grounded contract instead of dropping validation consumers merely to
   make generation faster.
3. Allow 1,800 seconds for the adapter boundary. Either built-in route retains
   120 seconds for serialization and process cleanup.
4. Preserve fail-fast semantics: a model or adapter timeout remains one
   infrastructure failure, never provider evidence or a consumed retry.

### Simplicity boundary

Do not add an ISV-facing timeout option, provider-specific timeout, partial
unverified checkpoint, or context truncation rule. One shared internal policy
covers both built-in generators.

### Verification

- Focused tests bind each adapter to its centrally defined model deadline and
  require the outer boundary to retain 120 seconds beyond either route.
- `uv run python -m unittest discover -s tests`: 152 tests passed.
- Ruff, Python compilation, lock validation, schema parsing, and diff checks
  passed.

## Step 42 - Localize Consumer Evidence and Bound Repeated Failures

### Evidence

The same BCM `describe_instance.py` adapter contract supplied 16 relevant
validation classes as complete source bodies. Its serialized context pack was
167,873 characters and the upstream-contract item alone was about 87,000
characters. Three generations produced no statically verified change before
the existing retry budget parked that contract. Whole consumer implementations
mixed provider requirements with unrelated test mechanics and made every retry
pay for the same material.

The completed bare-metal review then exposed two false-positive static passes.
The proposed launch adapter only inspected a pre-existing node and omitted five
fields consumed by downstream configured steps. The proposed serial-console
adapter waited for interactive `rconsole` to exit naturally and classified its
expected continued session as a timeout failure.

### Decisions

1. Replace complete validation-class bodies in each generation request with a
   deterministic AST interface projection. Preserve method signatures,
   docstrings, class attributes, keyed lookups and defaults, branch conditions,
   pass/fail outcomes, direct dependencies, caught exceptions, exact line
   ranges, class-source hashes, and explicit dynamic-call uncertainty.
2. Keep exact selected suite entries and step-output schemas unchanged. The
   pinned consumer file remains the source of truth at the recorded path and
   hash; the projection does not authorize edits to that source.
3. Resolve one source hop for uniquely named local helper functions called by
   the selected consumers. Include at most 4,000 characters per helper and do
   not recursively expand the call graph.
4. Represent candidate failures as structured records containing attempt,
   category, stable fingerprint, summary, and details. Send the latest record
   in full plus a compact ledger instead of concatenated prose.
5. Park one adapter contract when the same deterministic failure fingerprint
   occurs twice. Different failures may still use the configured three-attempt
   budget. Model and adapter infrastructure errors remain fail-fast and outside
   the repair ledger.
6. Retain the current model-neutral character ceiling. Codex and Claude do not
   share one tokenizer, so adding an approximate universal token gate would
   create false precision; structural localization is the immediate safe gain.
7. Treat interactive consoles and shells as sessions. Generated probes must
   use source-backed readiness evidence and terminate cleanly instead of
   interpreting a healthy session that stays open as a timeout failure.
8. Keep provider-authoring rules once at the generator task boundary. Do not
   serialize the identical rule set again inside the evidence pack.
9. Reject a recognizable literal result mapping when it omits a field consumed
   by a later configured step, not only when that field is present but empty.
   Defer when dynamic result construction prevents a deterministic conclusion.
10. Preserve lifecycle verbs explicitly: create, launch, provision, delete, and
   teardown steps cannot be satisfied by observing a pre-existing resource.

### Simplicity boundary

Do not add embeddings, a vector database, a retrieval service, an ISV-facing
advanced mode, or provider-specific extraction. The one-shot generator receives
the smallest deterministic evidence already available locally and parks when
that evidence cannot support a safe implementation.

### Verification

- Focused tests cover source provenance, keyed inputs, explicit dynamic
  uncertainty, structured retry feedback, and the two-identical-failure stop
  rule across independent adapter contracts.
- Replaying the BCM contract retained all 13 selected suite entries, the output
  schema, and all 16 validation interfaces with zero missing, omitted, or
  truncated items. After adding 11 exact one-hop helper sources, the serialized
  pack is 154,812 characters versus the prior 167,873; the upstream-contract
  item is 74,209 characters while also adding explicit return-expression and
  helper-behavior coverage.
- Replaying all 13 current BCM adapter contract units produced no packing
  failures, missing validation classes, omissions, or truncation. The
  16-interface `describe_instance.py` contract remained the largest pack.
- Rescanning the rejected BCM launch candidate now reports `key_file`,
  `key_name`, `public_ip`, `security_group_id`, and `vpc_id` as unpopulated
  downstream outputs for both launch-step validation rows.
- `uv run python -m unittest discover -s tests`: 154 tests passed.
- Ruff, Python compilation, lock validation, schema parsing, and diff checks
  passed.

## Step 43 - Invalidate Stale Local Qualification Context

### Evidence

The BCM review was rejected without applying its candidate patch. Rechecking
the authoritative product manuals showed that the local fixture itself had
incorrect collection envelopes and ambiguous lifecycle semantics. Editing that
fixture would not have changed later generator requests because cache freshness
checked only source IDs, not source declarations or local content.

### Decisions

1. Bind context-cache schema `0.3.0` to the complete declared context-source
   configuration and the per-source cached record.
2. When `qualify` checks cache freshness, resolve each local source from the
   project manifest and compare its current redacted content hash and origin
   with the indexed record. Refresh before proposal generation when either
   differs.
3. Keep network freshness explicit. A cache check never fetches a URL; network
   sources retain the last successful import until the normal sync path runs.
4. Do not add BCM response fields, state names, certificate behavior, or
   provisioning assumptions to generic guardrails. Those facts belong in the
   ISV-supplied authoritative contract and SME scope decision.

### Simplicity boundary

Do not keep hardening generic rules from one provider rehearsal. Add a generic
deterministic check only for a provider-neutral invariant with low false-positive
risk. Provider semantics and missing product interfaces must remain explicit
qualification blockers instead of being guessed by code or repaired through
repeated generation.

### Verification

- The context test changes a local API specification after sync, proves the
  cache becomes stale, refreshes it, and proves it becomes current again.
- `uv run python -m unittest discover -s tests`: 154 tests passed.
- Ruff, Python compilation, lock validation, schema parsing, and diff checks
  passed.

## Step 44 - Pre-validation Review-State and Application Audit

### Evidence

Before restarting the synthetic BCM validation, a full review of the recent
generation, review, timeout, and application paths found two state-integrity
problems. A generator infrastructure failure could leave an older
`auto-review.json` or `auto-review.patch` in the work directory, so an external
observer could mistake stale output for the failed run's result. The combined
review apply path also replaced each file atomically but did not roll back
earlier files when a later replacement failed, despite describing the whole
multi-file patch as atomic. The older change-set transaction retained a staged
temporary file if `os.replace` failed because it removed that path from its
cleanup map before the replacement completed.

An uncommitted rehearsal-only generator instruction was also rejected during
the audit. Allowing a declared existing-resource mode to satisfy a mutating
lifecycle step would require a deterministic non-production evidence gate
before readiness or publication. The product has no such gate, so the simpler
and accurate rule remains that launch/create/provision/delete/teardown preserve
their literal upstream meaning. A synthetic fixture may describe its limits,
but it cannot weaken the product's publishable validation contract.

### Decisions

1. Clear prior review artifacts immediately before a new scratch generation
   begins. A timeout or other generator failure now leaves no review result
   rather than preserving unrelated stale output.
2. When a completed review has no patch, explicitly remove any old patch file
   while writing the current JSON result.
3. Stage and fsync every reviewed file before replacing any provider target.
   Preserve existing file modes, back up every existing target, verify each
   reviewed after-hash, and roll back all earlier replacements if any later
   replacement or hash check fails.
4. Retain a staged path in the cleanup map until `os.replace` succeeds in both
   application implementations, so failed replacement attempts do not leak
   temporary files.
5. Keep provider-specific rehearsal semantics outside generic guardrails. No
   new profile state, exception mode, CLI flag, or BCM rule was added.

### Verification

- Focused tests cover stale-review removal on model timeout, multi-file rollback,
  file-mode preservation, and staged-file cleanup after a failed replacement.
- `uv run python -m unittest discover -s tests`: 156 tests passed.
- Ruff, Python compilation, every packaged JSON schema, lock validation, and
  diff checks passed.
- The pinned local `ai-cloud-validation` checkout's full `isvtest` suite passed
  all 1,271 tests, including the private BCM head-hop overlay's 14 SSH tests.
