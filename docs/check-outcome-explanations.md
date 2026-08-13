# Check outcome explanations

After each completed live domain run, `gapctl` writes
`.gapctl/runs/<run-id>/run-explanations.json`. This is a deterministic companion
to JUnit, not an agent-authored summary.

The artifact combines:

- the dynamic check result and JUnit reason;
- the scanner's redacted evidence message;
- the central blocking/edit decision;
- the reviewed solution-profile scope and rationale; and
- hashes of the profile, source report, JUnit, and log used for the record.

## Fields to consume

Each entry under `checks` contains:

| Field | Meaning |
| --- | --- |
| `check_id` | Stable row identifier from `gaps.json` for this result. |
| `domain`, `step_name`, `validation_class`, `requirement_id` | Check identity and lifecycle location. |
| `outcome` | `pass`, `fail`, `not_implemented`, `skipped`, or `error`. |
| `source` | `junit` for an executed testcase or `reviewed_scope` for an approved exclusion not executed by JUnit. |
| `reason_code` | JUnit reason when supplied; otherwise a deterministic fallback such as `validation_passed`, `validation_failed`, `runtime_skip`, or `approved_scope_exclusion`. |
| `explanation` | Redacted JUnit/scan message, or the reviewed scope rationale for an approved exclusion. |
| `validation_message`, `junit_testcase` | Original redacted validation detail and testcase identity when available. |
| `scope` | Coverage, ownership, action, rationale, and evidence references from the reviewed solution profile. |
| `decision` | The same blocking, edit-eligibility, action, and reason used by validation and readiness. |

The top-level `run` section binds the document to its run ID, provider, domain,
pinned validation commit, reviewed profile hash, source report hash, exit code,
and live success verdict. `artifacts` contains relative paths and hashes for the
canonical JUnit and redacted log.

## Outcome interpretation

- A `pass` explanation states what the JUnit/scanner evidence recorded. When a
  testcase emits no message, the fallback is the validation's deterministic
  pass summary; the document does not invent provider observations.
- A `fail` or `error` includes the redacted validation message and remains
  blocking according to the central decision.
- A runtime `skipped` result keeps the JUnit reason and is normally blocking
  until the reviewed profile resolves it.
- An approved product exclusion uses `source: reviewed_scope` and
  `reason_code: approved_scope_exclusion`; its explanation and evidence refs
  come directly from the reviewed profile.

The artifact is created for unsuccessful as well as successful live runs. The
current Lab Service publication path still uploads canonical JUnit only;
`run-explanations.json` is retained locally as the structured integration
contract for consumers that need richer outcome reasoning.
