# ISV Readiness

`isv-readiness` helps an infrastructure ISV move from initial capability
discovery to reproducible NVIDIA `ai-cloud-validation` results. It models the
complete solution, creates or discovers provider wiring, finds static coverage
gaps, ingests dynamic results, and routes each gap according to ownership.

`ai-cloud-validation` remains the source of truth for configuration validity
and pass/fail. This repository consumes its supported CLI contracts and never
edits provider-agnostic suites or validation-engine code.

## Current Status

Implemented:

- Two-phase model: **qualify** (assess and scope the ISV-owned domains) then
  **validate** (test the owned scope). An ISV validates whatever it owns —
  one domain or many.
- Pinned `isv-project.yaml` workspace bootstrap with the ISV-owned domains,
  API/spec references, and execution policy.
- Redacted, network-opt-in context synchronization for local files, API specs,
  public documentation, GitHub issues, and host-exported MCP content.
- Bounded per-gap context packs with source trust, hashes, credential-name-only
  availability, and relevance filtering.
- Explicit command-based generator adapter with a strict JSON/stdin contract
  and hash-bound multi-file change-set output.
- Reference `gapctl-codex-generator` adapter that uses Codex ephemeral,
  schema-constrained, read-only execution in an empty temporary directory.
- Transactional multi-file proposal, isolated static verification, backups,
  drift checks, explicit application, and rollback-on-application-failure.
- Policy-gated targeted/full-domain live runs with pinned-checkout enforcement,
  minimal runtime environments, redacted logs, JUnit ingestion, and explicit
  rollback.
- Persistent `agent-run` orchestration across scan, context, generation, review,
  apply, targeted live feedback, final full-domain validation, and retry gates.
- Sanitized, hash-inventoried owned-scope validation evidence bundles.
- Versioned `solution-profile.json` contract for components, dependencies,
  actors, NSRG layers, domains, ISV ownership (`owned`), and validation mode.
- Draft BCM 11 and NVIDIA Mission Control 2.3 reference profiles based on
  current public product documentation.
- Cross-domain provider onboarding through `isvctl provider scaffold`, plus
  completion of the known Kubernetes top-level wrapper gap.
- Version-aware validation plan export through `isvctl catalog list --json` and
  `isvctl test run --dry-run --no-upload`.
- Static scans across all current suite domains and every supported validation
  YAML shape.
- Dynamic JUnit ingestion for any single domain, with specialized K8s ownership
  classification and setup-inventory parsing.
- Profile-aware action routing that can suppress unsafe automatic edits.
- Masked-failure reconciliation: a failing scan result on an ISV-owned domain
  outranks an ownership claim that would skip it, and is surfaced (in the
  scorecard and `auto` review) for a scope decision instead of being hidden.
- Flat, schema-valid `gaps.json` reports and scorecard/tree/Markdown views.
- `gapctl auto`: an autonomous fill-and-fix loop that stages every owned,
  auto-fixable gap into an isolated scratch copy, verifies each in isolation,
  and stops at one hash-bound combined-diff review gate before touching source.
- Persistent, deterministic one-gap-at-a-time loop state with blocker-first
  routing and explicit retry accounting.

Not implemented yet:

- Additional model-vendor-native adapters; the command contract and Codex
  reference adapter are implemented.
- Pull-request submission; the change pipeline emits reviewed patches, not PRs.
- Publication workflows (NVIDIA AI Cloud Ready's "publish" stage). The current
  boundary ends with a reproducible owned-scope validation bundle.
- Integrated multi-owner full-stack roll-up (an NCP/integrator concern that
  aggregates several validated single-owner profiles), which is out of scope
  for a single ISV.

See [docs/architecture.md](docs/architecture.md) for the complete agentic design
and implementation map.

## Workflow

### 0. Bootstrap a scoped workspace

Preview a workspace that will clone and pin `ai-cloud-validation`:

```bash
gapctl bootstrap \
  --workspace /path/to/acme-readiness \
  --provider-name acme \
  --domains vm,network,k8s \
  --api-base-url https://api.acme.example/v1 \
  --api-base-url-env ACME_API_BASE \
  --api-spec /path/to/openapi.yaml \
  --auth-env ACME_CLIENT_ID \
  --auth-env ACME_CLIENT_SECRET
```

The command is a dry run until `--write` is supplied. It clones only when the
checkout is absent, resolves the checkout to an exact commit, and writes
`isv-project.yaml`. The manifest stores credential environment-variable names,
never credential values. Live runs are disabled by default.

Use `--validation-root /existing/ai-cloud-validation` to adopt an existing
checkout without pulling or changing its branch.

Bootstrap is the start of the **qualify** phase. It also creates a draft
profile from the domains the operator declared ISV-owned. An SME should review
product versions and capability-level ownership before the profile advances to
the `validate` journey stage. A profile supplied with `--profile` must cover
every owned domain. Qualification decides *what* is owned and in scope;
validation then *tests* that owned scope — an ISV is never expected to test
layers it does not own.

### 1. Qualify the solution

Start from a reference profile or create one for the candidate stack.

```bash
gapctl profile --in examples/profiles/bcm.reference.yaml
gapctl profile --in examples/profiles/nvidia-mission-control.reference.yaml
```

Reference profiles are discovery baselines, not product certifications. They
intentionally leave tenant control-plane, IAM, image-registry, SDN, and security
ownership unresolved until the integrated stack is confirmed.

### 2. Onboard a provider

For a complete provider scaffold, choose domains directly:

```bash
gapctl onboard \
  --provider-name acme \
  --validation-root /path/to/ai-cloud-validation \
  --domains bare_metal,k8s,slurm,observability
```

Or derive covered/testable domains and intake questions from a profile:

```bash
gapctl onboard \
  --provider-name bcm-lab \
  --validation-root /path/to/ai-cloud-validation \
  --profile examples/profiles/bcm.reference.yaml
```

Both commands are dry runs until `--write` is supplied. The broad flow delegates
creation to the authoritative command:

```text
isvctl provider scaffold <provider-name>
```

The existing lightweight K8s-only flow remains available:

```bash
gapctl onboard \
  --domain k8s \
  --provider-name dsx-air \
  --validation-root /path/to/ai-cloud-validation \
  --write
```

### 3. Export the authoritative plan

Before running a suite, merge and validate its configuration and join it to the
installed catalog:

```bash
gapctl plan \
  -f isvctl/configs/providers/k3s.yaml \
  --validation-root /path/to/ai-cloud-validation \
  --out validation-plan.json
```

The plan records suite/config/catalog versions, fingerprints, lifecycle steps,
validation categories, repeated variants, execution adapters, and malformed or
unknown entries. Unknown entries remain visible.

### 4. Scan static coverage

Scan explicit domains:

```bash
gapctl scan \
  -p /path/to/provider \
  --domains vm,network \
  --validation-root /path/to/ai-cloud-validation \
  --out gaps.json
```

Or derive domains and responsibility routing from a solution profile:

```bash
gapctl scan \
  -p /path/to/provider \
  --profile examples/profiles/bcm.reference.yaml \
  --validation-root /path/to/ai-cloud-validation \
  --out gaps.json
```

Static scanning detects missing configs and commands, missing scripts,
TODO/Not-implemented markers, skipped steps, simple literal JSON output schema
failures, and contract drift. A static `pass` means only that no static gap was
found; it does not replace a real validation run.

### 5. Synchronize context and build one minimal context pack

Declared network sources are never fetched implicitly:

```bash
gapctl context-sync \
  --project isv-project.yaml \
  --allow-network
```

This can cache relevant public NSRG material and open
`NVIDIA/ai-cloud-validation` GitHub issues. `GITHUB_TOKEN`, when present, is
used only in the request header and is not cached. A host agent with an
authorized MCP/Confluence connector can export selected material and import it
through the same redaction boundary:

```bash
gapctl context-import \
  --project isv-project.yaml \
  --source-id nvidia_ai_cloud_ready \
  --in /path/to/host-export.json
```

Build the bounded input for one selected row:

```bash
gapctl context-pack \
  --project isv-project.yaml \
  --in gaps.json \
  --gap-id gap_0123456789ab \
  --out context-pack.json
```

Executable validation contracts and provider API specifications are
authoritative. Reference implementations and NSRG material are guidance.
GitHub issues and MCP exports are advisory and cannot change scope, ownership,
or pass/fail results.

Run an explicitly chosen model adapter after reviewing the context pack:

```bash
gapctl generate \
  --context-pack context-pack.json \
  --generator gapctl-codex-generator \
  --out changes.json

gapctl change-propose \
  --in gaps.json \
  --provider-repo /path/to/provider \
  --changes changes.json \
  --patch-out proposal.patch \
  --out proposal.json

gapctl change-verify \
  --in gaps.json \
  --provider-repo /path/to/provider \
  --changes changes.json \
  --validation-root /path/to/ai-cloud-validation \
  --out change-verification.json
```

The generator runs without a shell and receives only an explicit environment
allowlist. Its output may touch up to 12 files, but the deterministic guard
requires the selected target and restricts additional edits to provider scripts,
the selected domain config, and the exact Kubernetes wrapper.

The Codex reference adapter invokes `codex exec` with `--ephemeral`,
`--ignore-user-config`, `--sandbox read-only`, `--output-schema`, and an empty
temporary working directory. Other model hosts can implement the same stdin /
stdout contract and be selected with `--generator`.

Apply a successful manifest only after reviewing the patch:

```bash
gapctl change-apply \
  --in gaps.json \
  --provider-repo /path/to/provider \
  --changes changes.json \
  --verification change-verification.json \
  --backup-dir backups \
  --out application.json \
  --apply
```

An explicit rollback is hash-gated against the application result:

```bash
gapctl change-rollback \
  --application application.json \
  --provider-repo /path/to/provider \
  --out rollback.json \
  --rollback
```

### 5.5. Run the gated agent workflow

`agent-run` persists one domain's state and stops at review and infrastructure
boundaries. It can scaffold a missing provider when `--onboard` is explicitly
supplied:

```bash
gapctl agent-run \
  --project isv-project.yaml \
  --domain vm \
  --work-dir .gapctl/agent/vm \
  --onboard \
  --generator gapctl-codex-generator
```

After reviewing the emitted patch, bind approval to the printed hash:

```bash
gapctl agent-run \
  --project isv-project.yaml \
  --domain vm \
  --work-dir .gapctl/agent/vm \
  --generator gapctl-codex-generator \
  --approve-patch <exact-sha256> \
  --apply \
  --run-live
```

Live execution requires `execution.allow_live_runs: true` in the reviewed
project. Only declared credential/runtime environment names are passed to
`isvctl`; API endpoints can be injected through an API's `base_url_env`. The
targeted selection uses the installed validation class with upstream `pytest
-k`, while `isvctl` still owns setup, test, and teardown. After all static gaps
are resolved, one final `agent-run --run-live` executes the entire domain before
the state becomes `complete`.

Assemble completed domain states without copying raw context, API specs,
credentials, model payloads, backups, or logs:

```bash
gapctl bundle \
  --project isv-project.yaml \
  --agent-work-dir .gapctl/agent/vm \
  --out-dir readiness-bundle
```

### 5.6. Run one live selection directly

The gated `agent-run`/`auto` flows are the recommended way to execute live, but
a single policy-authorized run can also be invoked directly. Like `agent-run`,
it requires `execution.allow_live_runs: true` in the reviewed project and the
explicit `--run-live` flag:

```bash
gapctl live-run \
  --project isv-project.yaml \
  --domain vm \
  --selection <validation-class> \
  --artifacts-dir .gapctl/live/vm \
  --out live-run.json \
  --run-live
```

`--selection` is passed to upstream `pytest -k`; omit it to run the whole
domain. Kubernetes runs additionally accept `--scope` for ownership
classification. Without `--run-live` the command previews the authorized
command without executing it.

### 6. Run or ingest one dynamic domain

Run a configured domain in place:

```bash
gapctl scan \
  -p /path/to/provider \
  --domains vm \
  --validation-root /path/to/ai-cloud-validation \
  --run \
  --out vm-gaps.json
```

Or ingest artifacts captured by the ISV:

```bash
gapctl scan \
  -p /path/to/provider \
  --domains vm \
  --validation-root /path/to/ai-cloud-validation \
  --junit /path/to/junit.xml \
  --log /path/to/isvctl.log \
  --out vm-gaps.json
```

Dynamic execution and artifact ingestion intentionally accept one domain per
invocation. This keeps config paths, JUnit cases, rerun commands, and retry
budgets unambiguous.

Kubernetes additionally accepts `--setup-json` and `--scope` for inventory and
layer-aware ownership classification.

### 7. Review the report

```bash
gapctl report --in gaps.json --format scorecard
gapctl report --in gaps.json --format tree
gapctl report --in gaps.json --format md
```

When a profile is supplied, each row includes a `solution_profile` enrichment
with a deterministic action:

- `implement_or_fix_adapter`
- `request_external_adapter`
- `collect_evidence`
- `record_product_gap`
- `skip_with_rationale`
- `request_scope_decision`

### 8. Autonomously fill and fix, then stop at one review gate

`gapctl auto` is the simplified end-to-end flow. It scans one owned domain,
then for every ISV-owned, scanner-`auto_fixable` gap it builds a context pack,
runs the generator, and verifies the candidate in an isolated copy of the
provider. Verified fixes are staged into a private scratch copy — the real
provider repository is never touched — and the loop rescans so later gaps see
earlier fixes. It stops at a single combined diff:

```bash
gapctl auto \
  --project isv-project.yaml \
  --domain vm \
  --work-dir .gapctl/auto/vm \
  --generator gapctl-codex-generator
```

The command reports the staged fixes, the gaps it parked, and where the
combined patch was written. Parked gaps include anything that needs a human
route (external adapter, evidence, scope decision) and any **masked failure** —
a check the deterministic scan reports as broken on an owned domain that the
profile tried to skip. The validation result outranks the ownership claim, so
these are surfaced for a scope decision rather than silently filled.

After reviewing `.gapctl/auto/vm/auto-review.patch`, bind approval to the exact
printed combined-patch hash to apply every staged file atomically with backups:

```bash
gapctl auto \
  --project isv-project.yaml \
  --domain vm \
  --work-dir .gapctl/auto/vm \
  --generator gapctl-codex-generator \
  --apply \
  --approve-patch <exact-sha256>
```

Only ISV-owned domains and gaps routed to `implement_or_fix_adapter` are
eligible; the owned-domain scope filter and the same guardrails as the manual
change pipeline still apply. `auto` covers static gaps; dynamic gaps still
require a reviewed live `isvctl` rerun via `agent-run --run-live`. For per-gap
review checkpoints instead of one end gate, the advanced `agent-run` workflow
remains available.

### 9. Advance the deterministic loop

The loop controller selects one blocker or fixable gap and persists its state;
it does not execute the row's rerun string:

```bash
gapctl loop \
  --in gaps.json \
  --domain vm \
  --state loop-state.json \
  --max-attempts 3
```

After a reviewed patch attempt and rescan, record that explicit attempt while
advancing with the new report:

```bash
gapctl loop \
  --in rescanned-gaps.json \
  --domain vm \
  --state loop-state.json \
  --attempted-gap gap_0123456789ab \
  --max-attempts 3
```

States are `ready`, `blocked`, or `complete`. Scope decisions, external
adapters, evidence requests, product gaps, non-authorized targets, and exhausted
retry budgets stop the controller rather than falling through to code changes.

## Contracts

- [schemas/project.schema.json](schemas/project.schema.json): pinned workspace,
  selected assessment scope, API/context declarations, and execution policy.
- [schemas/context-pack.schema.json](schemas/context-pack.schema.json): bounded,
  redacted, source-ranked input for one selected gap.
- [schemas/change-set.schema.json](schemas/change-set.schema.json): generated,
  context-bound multi-file provider changes.
- [schemas/change-proposal.schema.json](schemas/change-proposal.schema.json):
  guarded combined patch and per-file before/after hashes.
- [schemas/change-verification.schema.json](schemas/change-verification.schema.json):
  isolated rescan result for the complete transaction.
- [schemas/change-application.schema.json](schemas/change-application.schema.json):
  applied files and durable backup locations.
- [schemas/change-rollback.schema.json](schemas/change-rollback.schema.json):
  explicit restoration/removal evidence.
- [schemas/live-run.schema.json](schemas/live-run.schema.json): policy-authorized
  command, artifacts, selected outcomes, and normalized report.
- [schemas/agent-state.schema.json](schemas/agent-state.schema.json): persistent
  review/live/retry state for one selected domain.
- [schemas/bundle-manifest.schema.json](schemas/bundle-manifest.schema.json):
  sanitized artifact and provider-file hash inventory.
- [schemas/gaps.schema.json](schemas/gaps.schema.json): deterministic flat scan
  and dynamic-result rows.
- [schemas/validation-plan.schema.json](schemas/validation-plan.schema.json):
  normalized, version-aware `isvctl` plan.
- [schemas/solution-profile.schema.json](schemas/solution-profile.schema.json):
  versioned solution graph and responsibility model.
- [schemas/loop-state.schema.json](schemas/loop-state.schema.json): persistent
  selection, routing, retry, fingerprint, and history state for the controller.

`gaps.json` stays flat so the loop controller can select one row deterministically.
Profile data is enrichment and guardrail context; it does not rewrite test
outcomes.

## Kubernetes Wrapper Note

The current upstream provider scaffold creates
`providers/<name>/scripts/k8s/`, but not a matching
`providers/<name>/config/k8s.yaml`. Existing K8s providers use top-level files
such as `providers/k3s.yaml` that import `suites/k8s.yaml` and point commands at
provider scripts. Cross-domain onboarding preserves scaffolded scripts and adds
that wrapper plus an ownership-scope template.

## Dependency Boundary

The Python package depends only on JSON Schema and YAML libraries. Validation is
integrated through a supported external CLI boundary:

1. Preferred development flow: pass a sibling checkout with
   `--validation-root`. Its existing `.venv/bin/isvctl` is used when present,
   otherwise the tool runs `uv run isvctl` in that checkout.
2. Installed flow: install `ai-cloud-validation` separately so `isvctl` is on
   `PATH`, then use adapter APIs without a checkout root.

This avoids importing private validation internals and avoids trying to install
the multi-project `ai-cloud-validation` workspace as one editable setuptools
package.

## Safety Boundary

- Never edit `ai-cloud-validation` suites, validation classes, or engine code.
- Provider and product changes remain human-gated.
- A profile may disable `auto_fixable`; it can never enable an edit that the
  deterministic scanner marked unsafe.
- Future generation is limited to provider scripts and explicitly approved
  integration manifests, with patch/PR output rather than auto-merge.
- Credentials and private source remain in the ISV environment. Only reports
  and reviewed patches need to leave that boundary.

## Development

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Validate all schemas and compile Python sources:

```bash
python3 -m json.tool schemas/gaps.schema.json >/dev/null
python3 -m json.tool schemas/validation-plan.schema.json >/dev/null
python3 -m json.tool schemas/solution-profile.schema.json >/dev/null
python3 -m json.tool schemas/loop-state.schema.json >/dev/null
python3 -m compileall -q src tests
```
