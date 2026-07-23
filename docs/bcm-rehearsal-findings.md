# BCM Rehearsal: Fixes and Decisions

## Scope and outcome

BCM was exercised as a test ISV against the pinned `ai-cloud-validation`
contracts. This was a product rehearsal, not a BCM certification or publishable
AI Cloud Ready result.

| Domain | Result | What the run established |
| --- | --- | --- |
| Bare metal | Partial | Lifecycle, stable identity, SSH, OS, GPU recovery, storage IP, and host-health evidence worked. Physical-only and direct-host workload checks did not. |
| Kubernetes | Partial | Two one-GPU workers, GPU access, driver, labels, operator, networking, conformance, and control-plane metrics worked. NCCL and one following stress check failed. |
| Slurm | Pass | CPU, GPU, inline, and containerized GPU jobs passed; GPU stress completed 1,078 loops. |
| Observability | Pass | Reviewed telemetry-latency and host-syslog checks passed. |

Final rehearsal score: **90.5%**, with **276 of 305 scored rows passing** and
**29 gaps remaining**. Bare metal and Kubernetes therefore remain
non-publishable.

## `gapctl` fixes made during the rehearsal

| Problem found | Fix | Reason |
| --- | --- | --- |
| The ISV journey exposed too many internal commands. | Reduced the public workflow to `init`, `qualify`, `validate`, and `publish`; removed `bundle`. | The ISV should operate a qualification workflow, not orchestrate internal stages. |
| Qualification context could omit useful evidence or include GitHub issue noise. | Required complete catalogs, declared evidence, and all NCP guide pages; removed GitHub issues; fail closed on missing or oversized context. | Scope decisions must use authoritative, complete inputs. |
| Domain defaults could claim unsupported checks. | Made partial domains conservative and required semantic matches for each step and validation class. | Owning a domain is not proof of every capability in it. |
| Gaps were grouped because they edited the same config file. | Grouped by script or by configuration file plus configured step. | Unrelated lifecycle operations must be reviewed independently. |
| Generation lacked the exact downstream contract. | Added pinned suite entries, output schemas, relevant consumer code, and one bounded local helper hop. | The model must implement what the test consumes, not infer fields from names. |
| Large prompts and retries repeatedly timed out. | Localized evidence per adapter, cached it by source hash, used compact failure ledgers, and stopped repeated identical failures. | More transcript and longer timeouts did not improve correctness. |
| Generator and live commands could outlive cancellation. | Aligned timeout budgets and terminate/reap process groups on timeout, Ctrl+C, or termination. | A stopped run must not leave model or infrastructure processes behind. |
| Review rejection could resurface stale work. | Discarded rejected review state; made application transactional and resumable only for the exact verified patch. | Human review must apply exactly what was inspected. |
| Generated code could weaken security or result meaning. | Rejected undeclared inputs, TLS bypasses, insecure SSH, raw provider responses, placeholders, missing required outputs, and unsafe subprocess arguments. | Passing evidence must be secure, explicit, and source-backed. |
| Lifecycle timing and ordering were inconsistent. | Enforced source-backed timeouts and logical prerequisite/lifecycle order. | Alphabetical or size ordering can run dependent checks too early. |
| Skipped steps affected dependency decisions. | Ignored dependencies from profile-excluded steps and applied reviewed-scope overlays only at live-run time. | Excluded scope must not block included work or edit the suite. |
| Kubernetes config sat outside the provider review boundary. | Kept all generated domain configuration under the provider directory. | Static review and atomic apply need one provider-owned boundary. |
| Setup could succeed when only part of a declared resource set was ready. | Required the complete source-backed set, then emitted identities, counts, and capacities from that same set. | Partial readiness created false capacity and downstream failures. |

## BCM provider and lab fixes

- Used the BCM API with mutual TLS and certificate verification; credential
  values remained outside files and prompts.
- Accepted the source-backed list response shape from BCM power operations.
- Kept the head-node SSH hop as an explicit rehearsal-only overlay. It is not a
  normal ISV exception.
- Corrected lifecycle result mapping so an expected auxiliary-cycle failure did
  not suppress later independent evidence.
- Replaced inherited GPU defaults with the observed topology: two GPU workers,
  one H200 each, two GPUs total.
- Required full reprovisioning and bounded lifecycle waits where the upstream
  contract required them.
- Bounded journal output instead of returning unbounded host logs.
- Matched the real Slurm cluster and partitions and sized workloads for one GPU
  per node.
- Installed/configured Pyxis and Enroot for Slurm GPU containers. Exposed the
  existing `nvidia-container-cli` through the node image so the change survives
  reprovisioning.
- Required all declared Kubernetes GPU nodes and active GPU Operator components
  to be ready before setup succeeds.
- Kept Kubernetes workload placement adjustments in provider-owned
  configuration, not in NVIDIA suites.

## Remaining failures and ownership

| Finding | Classification | Decision |
| --- | --- | --- |
| Virtual lab lacks physical SOL, auxiliary power cycle, BMC, firmware/fabric proof, baseboard/CPU serials, and useful failure-domain metadata. | Target environment limitation | Keep as gaps; use representative physical infrastructure for final evidence. |
| Direct-host Docker, CUDA toolkit, PyTorch training, and GPU stress are absent although Slurm/Pyxis workloads work. | Target capability gap | Do not translate a Slurm container result into direct-host evidence. |
| Multi-node NCCL used sockets at about 0.04 GB/s and timed out without IB/RDMA. | Target networking gap | Add the required high-speed fabric or narrow the reviewed product claim. |
| The next Kubernetes stress pod ran before a timed-out MPIJob released its GPU. | Upstream suite cleanup defect | Fix bounded MPIJob/pod/GPU cleanup in `ai-cloud-validation`; do not hide it with a BCM delay. |
| Some profile-excluded checks still produced failing live rows. | Scope-review issue | Reconcile the reviewed profile with observed behavior before publication. |

## Decision rules retained

- Pinned executable contracts are authoritative; provider documents explain how
  to satisfy them; live results decide what the environment actually proved.
- A solution profile declares reviewed ownership and routing. It does not prove
  runtime support.
- Generated changes stay inside provider-owned files. NVIDIA suites remain
  read-only to `gapctl`.
- Success requires the complete expected resource set and its required state,
  not one positive sample.
- Classify failures as provider defects, target gaps, or upstream defects before
  changing guardrails.
- Stop adding guardrails when remaining failures are accurately classified
  target or upstream issues. Guardrails should prevent a demonstrated general
  failure mode, not encode one provider or make tests pass.

## Verification

- All 166 `isv-readiness` unit tests passed.
- Ruff, Python compilation, packaged JSON schemas, lock validation, and diff
  checks passed.
- All 506 private provider tests passed, including executable partial-resource,
  GPU Operator recovery, and timeout scenarios.
- Detailed decisions and evidence are recorded in `SLOP.md`, Steps 29-49.
