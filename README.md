# ISV Readiness

`isv-readiness` finds and closes gaps between an ISV provider implementation and NVIDIA's `ai-cloud-validation` bar, one domain at a time.

It is a separate repo. It depends one-way on `ai-cloud-validation`, reads provider configs and scripts, emits a deterministic `gaps.json`, and leaves `ai-cloud-validation` as the source of truth for pass/fail.

## What This Is

- A `gapctl` CLI for static v0.1 coverage/correctness scans.
- A clean `isv_readiness.scan` library with no agent, MCP, or model dependencies.
- A versioned flat gap report contract in `schemas/gaps.schema.json`.
- A first-stage scanner for provider repos shaped like `config/*.yaml` plus `scripts/<domain>/...`.

## What This Is Not

- Not a fork of `ai-cloud-validation`.
- Not the source of truth for validation pass/fail.
- Not allowed to edit provider-agnostic suites, validation classes, or the validation engine.
- Not an auto-merge system. Later fix loops create patch/PR output behind human review.

## Dependency Options

For the omnistation/sibling-checkout workflow, this repo is configured for uv with:

```toml
[tool.uv.sources]
ai-cloud-validation = { path = "../ai-cloud-validation", editable = true }
```

For a registry/package workflow, remove that block and resolve the package normally:

```toml
dependencies = [
    "ai-cloud-validation>=0.8.0",
    "jsonschema>=4.23.0,<5.0.0",
    "pyyaml>=6.0.2,<7.0.0",
]
```

The v0.1 scanner does not import validation internals. It opportunistically reads `isvctl.config.output_schemas` from the installed package or sibling checkout to validate static JSON samples.

## CLI

```bash
gapctl scan -p /path/to/provider-repo --domains vm,network
gapctl scan -p /path/to/provider-repo --domains vm --out gaps.json
gapctl report --in gaps.json --format scorecard
gapctl report --in gaps.json --format tree
gapctl report --in gaps.json --format md
```

Provider repo layout:

```text
provider-repo/
  config/
    vm.yaml
  scripts/
    vm/
      launch_instance.py
```

Kubernetes is a special case in the current `ai-cloud-validation` tree. The
`isvctl provider scaffold <name>` flow creates `providers/<name>/scripts/k8s/`,
but the scaffold does not currently create `providers/<name>/config/k8s.yaml`.
Existing local K8s providers are modeled as top-level files such as
`isvctl/configs/providers/k3s.yaml`, `microk8s.yaml`, and `minikube.yaml`; those
files import `suites/k8s.yaml` and override commands to point at provider
scripts. For a DSX Air pretend-ISV run, use the same pattern with a wrapper like
`isvctl/configs/providers/dsx-air.yaml` that points setup/teardown at
`providers/dsx-air/scripts/k8s/`.

When the sibling checkout is not adjacent to the provider repo, pass it explicitly:

```bash
gapctl scan -p /path/to/provider-repo --domains vm --validation-root /path/to/ai-cloud-validation
```

## Gap Contract

`gaps.json` is a flat list of rows so an agent can iterate it deterministically. The row spine is:

```text
domain + step_name + validation_class + requirement_id + milestone
```

Each row carries:

- `status`: `pass`, `fail`, `not_implemented`, `skipped`, or `error`
- `detection`: `static` in v0.1
- `stage`: `coverage` or `correctness`
- `gap_type`: routing for future fix/ticket behavior
- `evidence`: messages, schema errors, missing JSON fields, script/config paths
- `remediation`: auto-fix hint, edit target, rerun command, AWS reference
- `enrichment`: optional non-authoritative links

Static `pass` means the scanner found no static gap for that row. It does not replace a real `isvctl test run`.

## v0.1 Scanner Behavior

`gapctl scan` currently:

- Reads provider config steps for selected domains.
- Reads imported suites or falls back to `ai-cloud-validation/isvctl/configs/suites/<domain>.yaml`.
- Produces one row per suite validation check.
- Detects missing steps, missing scripts, TODO/Not implemented stubs, and skipped steps.
- Extracts simple literal JSON outputs from Python scripts and validates them against expected step output schemas.
- Adds AWS reference paths when a sibling `ai-cloud-validation` checkout is available.

The scanner never writes into the provider repo or into `ai-cloud-validation`.

## Roadmap

- v0.2: dynamic scan from validation JUnit and per-step JSON, requirement/milestone joins, richer scorecards.
- v0.3: guarded `gapctl fix` for one row and a Cursor `SKILL.md` wrapper.
- v0.4: deterministic `gapctl loop --until-green` with optional MCP enrichment hooks.
- v0.5: lib adoption module and service packaging.

## Development

Run the stdlib tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
