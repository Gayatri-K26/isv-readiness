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

- Versioned `solution-profile.json` contract for components, dependencies,
  actors, NSRG layers, domains, capability ownership, and validation mode.
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
- Flat, schema-valid `gaps.json` reports and scorecard/tree/Markdown views.

Not implemented yet:

- Frontier-model code generation for `gapctl fix`.
- Guarded patch creation and pull-request submission.
- The deterministic `gapctl loop --until-green` retry/stop controller.
- Publication workflows. The current boundary ends with a reproducible
  qualification or validation bundle.

See [docs/architecture.md](docs/architecture.md) for the complete agentic design
and implementation map.

## Workflow

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

### 5. Run or ingest one dynamic domain

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

### 6. Review the report

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

## Contracts

- [schemas/gaps.schema.json](schemas/gaps.schema.json): deterministic flat scan
  and dynamic-result rows.
- [schemas/validation-plan.schema.json](schemas/validation-plan.schema.json):
  normalized, version-aware `isvctl` plan.
- [schemas/solution-profile.schema.json](schemas/solution-profile.schema.json):
  versioned solution graph and responsibility model.

`gaps.json` stays flat so the future loop can select one row deterministically.
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
python3 -m compileall -q src tests
```
