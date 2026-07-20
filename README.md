# ISV Readiness

`gapctl` guides infrastructure ISVs through qualification and validation against
NVIDIA's [`ai-cloud-validation`](https://github.com/NVIDIA/ai-cloud-validation)
contracts.

The ISV-facing product has four commands:

```text
gapctl init -> gapctl qualify -> gapctl validate -> gapctl publish
```

There is no advanced CLI mode. Catalog building, context packing, static
scanning, generation, guardrails, verification, application, live execution,
evidence recording, and readiness assessment are internal steps owned by those
four commands.

## Distribution status

This repository is currently a development prototype. Do not direct external
ISVs to install an NVIDIA product from a contributor's personal GitHub
namespace. Before external release, transfer the source to an approved
NVIDIA-owned location and publish a versioned release.

The intended installation shape is:

```bash
uv tool install "git+https://<approved-repository>/isv-readiness.git@v0.1.0"
```

Installation provides `gapctl` and its Codex and Claude generator adapters. It
does not clone `ai-cloud-validation`; that happens during `gapctl init`.

## Complete ISV journey

### 1. Initialize

```bash
gapctl init acme-cloud \
  --workspace ./acme-readiness \
  --domains vm,network \
  --api https://api.acme.example/v1 \
  --api-spec /absolute/path/to/openapi.yaml \
  --auth ACME_CLIENT_ID \
  --auth ACME_CLIENT_SECRET \
  --input ACME_REGION

cd acme-readiness
```

`init`:

- validates the provider name, owned domains, and runtime environment-variable names;
- clones the selected `ai-cloud-validation` branch or tag when absent;
- records the exact validation commit in `isv-project.yaml`;
- builds the NVIDIA check catalog for the declared domains;
- scaffolds the provider-owned scripts and configuration;
- imports and redacts the declared API specification and the complete NVIDIA
  NCP Software Reference Guide from NVIDIA's published documentation index;
- creates the initial draft solution profile.

GitHub issues are not qualification sources. The pinned executable contracts,
the ISV's own evidence, the complete reference guide, and recorded runs are the
qualification inputs.

When `--api` is supplied, `validate` injects that value into provider scripts as
`ISV_API_BASE_URL`; the operator does not need to export it separately.

Use `--validation-ref <branch-or-tag>` to select something other than `main`.
A new workspace receives the current head of that ref and pins its commit.
Later commands never pull the checkout forward silently.

Credential values are never command arguments or project data. `--auth`
accepts names such as `ACME_CLIENT_SECRET`, not secret values.
`--input` declares a required non-secret runtime environment-variable name.
Repeat either option when the provider needs more than one input. Both are
recorded as names only and must be set before live validation.

### 2. Qualify

```bash
gapctl qualify
```

`qualify` compares the pinned NVIDIA contracts with the imported ISV evidence
and creates:

```text
.gapctl/qualification/solution-profile.proposed.yaml
```

The qualification pack keeps the suite catalogs, authoritative ISV evidence,
and NCP Software Reference Guide whole. The complete guide is a required
qualification source. If a page cannot be imported or the sources do not fit
the guarded context limit, `qualify` stops with an error instead of silently
continuing with missing, omitted, or truncated material.

The ISV SME reviews that file. Unresolved ownership or coverage decisions stop
the command and remain visible. Edit the proposal and run the same command
again; there is no separate profile, draft, or approval command.

Qualification does not treat a declared domain as proof that every check in
that domain is supported. A `covered/test` domain default is proposed only when
the supplied ISV evidence maps the complete pinned domain catalog. Partial
products receive grouped capability mappings, and unmatched checks remain an
explicit scope decision. Grouping is semantic rather than keyword-based: each
selected step-and-validation-class pair must be supported by the cited
interface behavior.

When the proposal is complete, `qualify` displays its hash and asks for explicit
approval. Approval promotes the exact reviewed proposal to
`solution-profile.yaml`, records it as reviewed, and enters the validate phase.
The generator may propose scope, but it cannot approve ownership or expand the
domains declared during `init`.

Claude can be selected without changing the workflow:

```bash
gapctl qualify --generator claude
```

### 3. Validate

Set the declared provider runtime values in the current environment before
live validation:

```bash
export ACME_CLIENT_ID="..."
export ACME_CLIENT_SECRET="..."
export ACME_REGION="us-west"

gapctl validate
```

For every owned domain, `validate`:

1. scans provider-owned files against the pinned NVIDIA contracts;
2. selects only gaps that are both profile-approved and deterministically safe
   to edit;
3. gives the selected generator the complete declared runtime-input contract,
   every edit-eligible unresolved check sharing the same provider target, and
   the exact pinned suite entries, step-output schemas, relevant validation
   class implementations, and a compact provider-adapter checklist; the
   bounded per-gap pack fails rather than silently truncating or omitting a
   selected source;
4. rejects undeclared runtime inputs, TLS-verification bypasses, raw
   provider output in result JSON, required downstream outputs left provably
   empty, and internal deadlines that exceed the configured step timeout;
5. verifies the candidate statically in an isolated copy;
6. writes one combined patch under `.gapctl/work/<domain>/`;
7. displays the patch hash and asks for human approval;
8. applies only the exact reviewed patch;
9. refuses to start live infrastructure while a known static blocker remains;
10. asks for explicit authorization before running the real cloud tests;
11. records JUnit, logs, run metadata, and the full-scope gap scorecard.

If review or live execution is deferred, run `gapctl validate` again. It resumes
an existing statically verified candidate patch instead of regenerating a
different patch before approval.

Claude can be selected with:

```bash
gapctl validate --generator claude
```

`validate` prints readiness after the run. Repeat the same command until every
owned domain has no blocking gaps and a successful live test. Gaps that are not
safe to generate are parked with a reason for SME or manual implementation. If
the supplied provider interfaces cannot satisfy an exact upstream contract, the
generator can park that contract with a specific blocker instead of inventing
a passing implementation.

Run artifacts are stored under:

```text
.gapctl/runs/<run-id>/
├── run.json
├── junit.xml
└── isvctl.log
```

Successful evidence from earlier domains is preserved while later domains run.

### 4. Publish

NVIDIA supplies the Lab Service endpoint, issuer, client credentials, and lab
ID. Keep the credential values in the process environment:

```bash
export ISV_SERVICE_ENDPOINT="..."
export ISV_SSA_ISSUER="..."
export ISV_CLIENT_ID="..."
export ISV_CLIENT_SECRET="..."

gapctl publish --lab-id 35 --isv-software-version 1.2.3
```

`publish` performs the same readiness assessment used by `validate` before any
remote mutation. It verifies the reviewed scope, full-scope gap report, latest
successful JUnit record for every owned domain, and the pinned validation
commit. It then creates one correctly typed Lab Service test run per owned
domain and uploads that domain's canonical JUnit XML.

There is no bundle step.

## Safety and ownership boundaries

- `ai-cloud-validation` is a read-only source of truth. Generated changes are
  limited to the selected provider-owned files.
- The ISV declares ownership; the model cannot infer it from lab access.
- A draft profile cannot authorize generation, application, live tests, or
  publication.
- Every generated patch is schema-constrained, statically verified in
  isolation, and bound to the bytes the human reviews. Static verification is
  not evidence that a provider API behaves correctly; only the live run proves
  that.
- Live cloud execution requires an explicit confirmation inside `validate`.
- Credentials are represented in durable files only by environment-variable
  name.
- A result covers only the declared ISV-owned domains. It is not proof that an
  entire multi-owner cloud stack is validated.
- DSX Air or another simulation proves workflow plumbing, not real-provider
  qualification.

## Workspace state

The main operator-visible artifacts are:

```text
acme-readiness/
├── ai-cloud-validation/        # pinned NVIDIA checkout
├── isv-project.yaml            # immutable scope inputs and checkout pin
├── solution-profile.yaml       # SME-reviewed ownership and coverage
├── gaps.json                   # latest full-scope readiness result
└── .gapctl/
    ├── catalog.json
    ├── context-cache/
    ├── qualification/
    ├── work/
    └── runs/
```

## Maintainer verification

The internal services remain directly unit-tested even though they are not
separate CLI commands:

```bash
uv run python -m unittest discover -s tests
uvx ruff check src tests
python3 -m compileall -q src tests
uv lock --check
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for the internal state and
safety boundaries behind the four commands.
