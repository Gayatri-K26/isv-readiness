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

Installation provides `gapctl` plus Codex and Claude reference adapters. Any
agent can participate through the same versioned adapter protocol, a locally
registered executable, or complete-request file exchange. Installation does
not clone `ai-cloud-validation`; that happens during `gapctl init`.

## Complete ISV journey

### 1. Initialize

```bash
gapctl init acme-cloud \
  --workspace ./acme-readiness \
  --domains vm,network \
  --context /absolute/path/to/reference-architecture.md \
  --context https://docs.acme.example/platform \
  --api https://api.acme.example/v1 \
  --api-spec /absolute/path/to/openapi.yaml \
  --auth ACME_CLIENT_ID \
  --auth ACME_CLIENT_SECRET \
  --input ACME_REGION

cd acme-readiness
```

`init`:

- validates the provider name, owned domains, and runtime environment-variable names;
- uses a supplied `ai-cloud-validation` checkout as-is, or clones the selected
  branch or tag when absent;
- records the exact validation commit in `isv-project.yaml`;
- builds the NVIDIA check catalog for the declared domains;
- preserves an existing provider implementation, or scaffolds provider-owned
  scripts and configuration when it is absent;
- imports and redacts every declared ISV context source, any optional API
  specification, the complete NVIDIA NCP Software Reference Guide from
  NVIDIA's published documentation index, and the complete NVIDIA Inference
  Reference Architecture;
- creates the initial draft solution profile.

`isv-project.yaml` is the only place source locations and import settings are
defined. `solution-profile.yaml` records ownership and coverage decisions and
may cite qualification evidence by `source_refs` or `evidence_refs`; those
values are IDs from the qualification pack, not duplicate source definitions.
Qualification rejects invented or unavailable citation IDs.

GitHub issues are not qualification sources. The pinned executable contracts,
the ISV's own evidence, both complete NVIDIA references, and recorded runs are
the qualification inputs.

When `--api` is supplied, `validate` injects that value into provider scripts as
`ISV_API_BASE_URL`; the operator does not need to export it separately.

The project manifest calls the collection `interfaces`, not `apis`, because a
provider interface may be `rest`, `graphql`, `cli`, `sdk`, `kubernetes`, or
`other`. The `--api` and `--api-spec` options are a REST convenience: when
present, `init` creates a `kind: rest` interface. Other interface kinds can be
declared under `interfaces` and their authoritative manuals, schemas, or command
references supplied through repeatable `--context` inputs. Older manifests that
still use the top-level `apis` key are accepted in memory and normalized to the
canonical interface model; new manifests and agent packs emit `interfaces`.

An API URL and API specification are optional. Use repeatable `--context`
arguments when the ISV instead supplies reference architectures, product or
operations documentation, command references, configuration examples, or
other qualification evidence. Each value may be a readable local file, a
directory of text documents, or an HTTP(S) URL. Explicitly supplied context is
required for that workspace: initialization stops if it cannot be imported
rather than qualifying against incomplete evidence.

Use `--validation-ref <branch-or-tag>` to select something other than `main`.
A new workspace receives the current head of that ref and pins its commit.
Later commands never pull the checkout forward silently.

If the ISV already has the repository, point `init` at it directly:

```bash
gapctl init acme-cloud \
  --workspace ./acme-readiness \
  --validation-root /absolute/path/to/ai-cloud-validation \
  --domains vm,network
```

The checkout must contain the upstream `my-isv` provider template. `init` does
not fetch, pull, switch branches, or overwrite an existing
`isvctl/configs/providers/acme-cloud` implementation. It records the checkout's
current commit; `--validation-ref` only selects the branch or tag for a new
clone.

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
NCP Software Reference Guide, and Inference Reference Architecture whole. Both
NVIDIA references are required qualification sources. The Inference Reference
Architecture is included for every ISV without a special flag. Its original
Mermaid diagrams are preserved and accompanied by deterministic prose
descriptions of their nodes, groups, and relationships. If a required page
cannot be imported, `qualify` stops instead of silently continuing with
missing material. Qualification has no separate character limit and never
omits or truncates pack items. The selected generator's configurable byte
capacity applies to the complete serialized request; an undersized adapter is
rejected before invocation. Older project manifests receive the standard
Inference Reference Architecture source when loaded; the manifest is not
silently rewritten.

The ISV SME reviews that file. Unresolved ownership or coverage decisions stop
the command and remain visible. Edit the proposal and run the same command
again; there is no separate profile, draft, or approval command.

If a generated proposal fails schema, reference, domain-set, or component-graph
validation, `qualify` gives the next bounded generator pass one redacted,
normalized failure envelope containing the exact structural error. A repeated
failure or exhausted project retry budget parks generation for triage instead
of persisting an invalid proposal.

The review output shows domain-default changes and then resolves every pinned
check through the proposed capability selectors. Per-domain and total counts
distinguish effective `covered/test`, `out_of_scope/skip`, and other outcomes,
so a partial-domain skip default is not mistaken for skipping its mapped
`covered/test` exceptions.

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

The selected agent is not part of ISV scope. Codex and Claude are built-in
aliases:

```bash
gapctl qualify --generator claude
```

Every generator receives the same repository-pinned `isv-readiness-agent`
skill with a qualification, remediation, or read-only domain-audit workflow.
The skill teaches the
agent how to reason from evidence; the existing schema, hash, scope, static
verification, review, and live-run gates remain deterministic `gapctl`
controls. Codex receives the skill natively in its isolated workspace. Other
adapters receive the identical versioned skill instructions in the request.

Any executable that reads one request JSON object from stdin and writes one
schema-valid response JSON object to stdout can be selected directly:

```bash
gapctl qualify --generator /opt/company/bin/gapctl-internal-agent
```

Callable adapters run from an empty temporary working directory. They must not
write the project, provider, pinned validation checkout, reviewed profile, or
scratch provider directly; gapctl fingerprints those protected paths around
each call and rejects any adapter that bypasses the JSON change-set protocol.
Use absolute paths for adapter scripts and other file arguments.

Reusable local adapters are registered outside the ISV project in
`~/.config/gapctl/generators.yaml` (or the path named by
`GAPCTL_GENERATORS_CONFIG`):

```yaml
generators:
  internal-agent:
    protocol_version: "0.1.0"
    command:
      - /opt/company/bin/gapctl-internal-agent
      - --profile
      - isv-readiness
    pass_env:
      - INTERNAL_AGENT_TOKEN
    timeout_seconds: 7200
    idle_timeout_seconds: 900
    max_request_bytes: 8000000
```

Only environment-variable names are configured. Values remain in the local
process environment. The total deadline may be configured up to eight hours;
an optional idle deadline is refreshed by adapter output, normally progress on
stderr. Model-specific correction attempts belong inside the adapter.

When the ISV's agent has no callable CLI, export the same complete request:

```bash
gapctl qualify --generator export
# Give .gapctl/qualification/generator-request.json to the available agent.
gapctl qualify --generator-response ./agent-response.json
```

The imported response must match the exact exported request and passes the same
schema, source, scope, and SME review gates. Validation change sets also remain
bound to their gap and context-pack hashes. The request is rejected before
invocation when it exceeds an adapter's declared capacity; required context is
never truncated.

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

1. scans provider-owned files against the pinned NVIDIA contracts, surfacing
   both config-level `skip: true` decisions and literal script-emitted
   `skipped: true` / `*_skipped: true` results for scope review;
2. selects only gaps that are both profile-approved and deterministically safe
   to edit, in declared phase and provider-step execution order rather than by
   target name or size;
3. gives the selected generator the complete declared runtime-input contract,
   every edit-eligible unresolved check in the same adapter contract unit, and
   a compact ordered map of every setup, test, and teardown step in the domain;
   the complete domain config, existing setup/teardown scripts, and existing
   provider-shared helpers are included so a file-level edit preserves the
   lifecycle and reuses one client;
4. requires direct authenticated Python transports to validate the base URL
   before attaching credentials: HTTPS and a hostname are mandatory, while
   userinfo, query, and fragment components are rejected; recognizable cleanup
   adapters must treat already-absent resources as success, avoid grouping
   independent deletes in one fail-fast block, and aggregate cleanup errors;
   generated Python must remain directly reviewable and cannot execute
   dynamically decoded or constructed code; shell lifecycle scripts cannot
   attach HTTP credentials directly and must invoke the provider-shared client;
5. includes the exact pinned suite entries and step-output schemas plus a
   deterministic, source-hashed interface projection of the relevant
   validation consumers and one bounded source hop for uniquely resolved local
   helpers they call; checks
   share a unit by script, or by configuration file plus step; the per-gap pack
   preserves every selected source whole, and the selected adapter's declared
   complete-request capacity is the only size gate;
6. rejects undeclared runtime inputs, TLS-verification bypasses, raw
   provider output in result JSON, required downstream outputs left provably
   empty or omitted from a recognizable result mapping, lifecycle timeouts below
   an explicit authoritative source threshold, internal deadlines that exceed
   the configured step timeout, and domain-config replacements that alter
   anything outside the selected step;
7. verifies the candidate statically in an isolated copy;
8. after deterministic gaps are exhausted, runs one read-only completeness
   audit that accounts for every approved `covered/test` capability and checks
   the complete existing domain lifecycle against the supplied profile,
   provider sources, survey, and API evidence;
9. turns an audit finding into the same guarded gap workflow instead of letting
   existing files or schema-valid output imply that lifecycle behavior exists;
   after a semantic candidate is staged, runs one post-change audit and parks
   any unresolved finding for the next reviewed pass;
10. retains `domain-audit.pre.json` and, when remediation occurred,
    `domain-audit.post.json` beside the domain review evidence;
11. writes one combined patch under `.gapctl/work/<domain>/`;
12. displays the patch hash and asks for human approval;
13. applies only the exact reviewed patch;
14. refuses to start live infrastructure while a known static or semantic
    blocker remains;
15. asks for explicit authorization before running the real cloud tests;
16. adds a run-local `isvctl` exclusion overlay for validation classes that the
    reviewed profile explicitly routes out of scope, without editing the pinned
    suite or provider source;
17. rejects live success when any emitted JUnit testcase records a `failure` or
    `error`, including injected subtests, regardless of process exit or summary
    text;
18. records JUnit, redacted logs, run metadata, a schema-backed explanation for
    every executed check and approved scope exclusion, and the full-scope gap
    scorecard.

All scaffolded domain configuration, including Kubernetes, lives under the
provider directory. The isolated review copy and atomic apply therefore use one
provider-owned filesystem boundary; legacy sibling Kubernetes wrappers remain
readable but are not generated for new workspaces.

Rejecting a candidate discards its generated review markers. The next
`gapctl validate` regenerates from the unchanged provider instead of repeatedly
showing the rejected patch.

Generator guidance also treats provider-derived subprocess arguments as
untrusted, preserves the declared meaning of optional response fields, and
requires interactive console or shell probes to terminate cleanly rather than
misclassifying a healthy continuing session as a timeout.
Lifecycle verbs also remain literal: a launch or create step cannot be replaced
with a read-only check of a pre-existing resource.
The completeness audit is a separate, schema-bound model call and cannot edit
files. Its response must cover every approved capability exactly once, cite
supplied provider files for an `implemented` verdict, and select an existing
provider-owned target for a `gap`. It is deliberately not presented as live or
cassette-based proof; the normal static, human-review, and live gates remain.
It reuses the complete domain context pack and a small relative-path index,
with both audit and remediation reading the evolving isolated provider copy.
The agent must reuse suitable provider transport, routing, polling, inventory,
and client primitives; cite the supplied source for each wire mapping; restore
safe test baselines after mutation; and require evidence that existing command
paths are executable through the declared route.
Retry requests contain the latest deterministic, redacted failure envelope plus
a compact attempt ledger, not prior candidates or an accumulated transcript.
The envelope records expected versus actual behavior, a stable error and one
representative excerpt per root cause, affected checks and counts, and paths
plus hashes for the complete retained artifacts. Fingerprints normalize
timestamps, UUIDs, labeled run IDs, and polling counters. A retry occurs only
when the observed row remains scanner-approved for the reviewed provider edit;
ambiguous evidence or an ownership conflict is parked for triage.
If the same normalized fingerprint appears twice, the contract is parked
without a third identical generation. `execution.max_failure_groups` controls
the distinct-root-cause ceiling and defaults to 10 for new and older manifests.
The reviewed qualification mapping is an implementation premise during
`validate`: runtime uncertainty is handled by response validation and explicit
failure, while refusal is reserved for absent interfaces and structural
incompatibility with the pinned NVIDIA contract.

If review or live execution is deferred, run `gapctl validate` again. It resumes
an existing statically verified candidate patch instead of regenerating a
different patch before approval.

The same built-in, registered, executable, and file-exchange routes apply to
validation:

```bash
gapctl validate --generator claude

gapctl validate --generator internal-agent

gapctl validate --generator export
# Give .gapctl/generator-request.json to the available agent.
gapctl validate --generator-response ./agent-response.json
```

File exchange imports at most one generated candidate per invocation so its
verified scratch change reaches the normal patch review gate before another
agent request is exported.

On macOS, gapctl passes the non-secret `USER` process identity to generator
adapters so Claude can retrieve an existing SSO session from Keychain. Claude
credentials remain outside gapctl and are never copied into project files or
generation context.

A model or adapter total/idle timeout stops the current validation run
immediately. It is an infrastructure failure, not evidence about an ISV
capability, so gapctl does not spend the provider retry budget repeating the
identical request. No real provider file is changed.

Live `isvctl` execution uses the same process-group cleanup boundary. A timeout,
Ctrl+C, or SIGTERM terminates and reaps the active validation process and its
provider-script children instead of leaving infrastructure pollers running.

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
├── isvctl.log
└── run-explanations.json
```

`run-explanations.json` is generated after the live run without another model
call. It combines each JUnit outcome with the redacted validation message, the
central blocking decision, and the reviewed coverage, ownership, rationale, and
evidence references. Approved scope exclusions that were intentionally omitted
from JUnit are included with `source: reviewed_scope`. See
[`docs/check-outcome-explanations.md`](docs/check-outcome-explanations.md) for
the field contract.

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

## Reference guides

- [ISV qualification runbook](docs/isv-qualification-runbook.md): exact
  operator commands from installation through publication.
- [BCM rehearsal fixes and decisions](docs/bcm-rehearsal-findings.md): concise
  results, product fixes, remaining gaps, and ownership decisions from the BCM
  test-ISV run.

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
- Failure summaries are diagnostic pointers, not replacements for evidence:
  complete run artifacts remain on disk and every envelope carries their paths
  and content hashes.
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
├── isv-project.yaml            # scope, source definitions, and checkout pin
├── solution-profile.yaml       # SME-reviewed decisions and source-ID citations
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
