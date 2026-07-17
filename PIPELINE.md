# gapctl Pipeline

`gapctl` is a set of composable CLI commands that pass state to each other
through JSON files on disk. Each command reads a file, does one deterministic
thing, and writes a file. A human (or a runbook) sequences them.

The pipeline has two phases: **qualify** (assess & scope the ISV-owned domains)
and **validate** (test the owned scope).

## Flow

```
QUALIFY  (assess & scope the ISV-owned domains)
─────────────────────────────────────────────────────────────
  bootstrap ──► isv-project.yaml   (pinned commit, live-runs OFF)
      │        solution-profile.yaml (DRAFT)
      ▼
  [ SME reviews profile, assigns NSRG layers ]   ◄── human gate
      │
      ▼
  profile ──► "validation-ready (owned scope): yes/no"


VALIDATE  (test the owned scope)
─────────────────────────────────────────────────────────────
  scan ──────────────────────────────────────► gaps.json
    static scan
      └─(optional --run)─► isvctl run ─► JUnit/log ─► merge dynamic rows
                                                          │
                                        profile enrichment (LAST)
                                        + masked-failure reconcile
      │
      ▼
  pick an orchestrator ───────────────┐
      │                               │
  ┌───┴──── auto ────────┐     ┌──────┴──── agent-run ──────┐
  │ (one review gate)    │     │ (a gate per gap)           │
  │                      │     │                            │
  │  loop in SCRATCH:    │     │  one TURN in work-dir:     │
  │   scan scratch       │     │   scan → pick 1 gap        │
  │   pick fixable gap   │     │   context-pack             │
  │   context-pack       │     │   generate (model adapter) │
  │   generate ──┐       │     │   change-verify            │
  │   verify     │ these │     │   review ──► agent-state   │
  │   stage      │ are   │     │      │                     │
  │  (repeat)    │ the   │     │      ▼                     │
  │      │       │ core  │     │  [approve hash] ◄─ human   │
  │      ▼       │ prims │     │      │                     │
  │  combined    │       │     │  --apply --approve-patch   │
  │  patch+hash  │       │     │      │                     │
  └──────┼───────┘       │     └──────┼─────────────────────┘
         │                            │
         ▼                            │
  [review patch, approve hash] ◄─ human
         │                            │
  --apply --approve-patch <sha>       │
         │                            │
         └──────────┬─────────────────┘
                    ▼
            provider source changed  (atomic, with backups)
                    │
                    ▼
  live-run  (needs allow_live_runs + --run-live)  ◄── policy gate
    isvctl targeted validation ─► artifacts
                    │
                    ▼
  test ──► canonical .gapctl/runs/<run-id>/ evidence
    │
    ├──► status ──► local readiness
    └──► publish ──► one Lab Service run per owned domain
```

## Linear stage list

```
bootstrap → profile → scan → context-pack → generate → verify → review → apply → test → status → publish
```

## Notes

- **Artifacts are the pipe.** `gaps.json` out of `scan` feeds the remediation
  stages; the combined patch and its SHA-256 are what `apply` consumes. State
  lives in files, not in a running process.
- **Two hard gates.** Nothing crosses `apply` without a hash-matched approval,
  and nothing reaches `live-run` without explicit authorization
  (`allow_live_runs` + `--run-live`).
- **The middle block loops.** `context-pack → generate → verify → apply` is not
  one-way: a failed `verify` sends it back to `generate` (retry budget).
- **Two orchestrators drive that block:**
  - `auto` — runs the inner cascade for every owned gap in a scratch copy, then
    stops at **one** review gate with a combined patch.
  - `agent-run` — runs one gap per turn, stopping at a review gate **per gap**.

  Same primitives underneath; they differ only in how often they stop.
- Within `scan`, an optional dynamic run (`scan --run`) executes isvctl and
  merges JUnit/log rows **before** profile enrichment, so masked-failure
  reconciliation sees runtime failures.
```
