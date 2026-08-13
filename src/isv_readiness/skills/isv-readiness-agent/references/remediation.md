# Remediation workflow

1. Read the selected gap, related target gaps, reviewed capability mapping,
   pinned consumers, output schemas, allowed edit boundary, and declared runtime
   environment names. Treat the reviewed mapping as an implementation premise.
2. Derive interface paths, methods, authentication, identifiers, lifecycle
   semantics, topology, and timing only from supplied evidence. Before coding,
   establish the complete wire contract: exact operation and discriminator
   literals, required request fields, response envelope and cardinality,
   success, pending, terminal, and failure states, polling operation and
   identifier, and error semantics. Identify the supplied source for each of
   those mappings. Fail closed when a required element is not evidenced; do
   not copy demo inputs or choose plausible values.
3. Design the smallest provider-owned change for the shared adapter contract.
   First inspect existing provider modules for transport, routing, polling,
   inventory, and shared clients; reuse or extend them instead of creating a
   second implementation unless evidence shows they cannot satisfy the
   contract. Preserve cross-step data flow, cleanup and error behavior, and
   unrelated configuration. Include the selected remediation target and only
   the additional provider-owned lifecycle scripts or selected domain config
   required by that same adapter contract. When authenticated HTTP is required, use exactly
   one provider-shared client under `scripts/common/<client>.py`; import it from
   lifecycle scripts rather than placing credential-bearing transport directly
   in a domain script or directly under `scripts/`. Before attaching any credential, that client
   must parse the base URL, require HTTPS and a hostname, and reject userinfo,
   query, and fragment components. Keep certificate and hostname verification
   enabled, normalize provider errors without response bodies, and implement
   bounded retry/backoff only for source-supported transient outcomes and
   operations that are idempotent or carry an evidenced idempotency mechanism.
4. Model lifecycle work as a state transition: establish the starting state,
   perform the requested operation, poll its authoritative state, then verify
   independent postconditions. Retry only when the operation is idempotent or
   evidence establishes safe transient recovery. Do not treat logs or a
   conveniently named status flag as authoritative unless the contract does.
   For a mutating test, capture the relevant starting configuration and health,
   restore them after success or failure when safe, and verify the shared
   resource returned to that baseline.
5. Preserve one canonical resource identifier across setup output, configured
   arguments, lifecycle calls, and result JSON.
   Cleanup may act only on identifiers proven to be owned by this run. Treat a
   documented already-absent result, commonly HTTP 404, as successful absence.
   Attempt independent cleanup actions even when one fails, collect stable
   redacted messages in `cleanup_errors`, and report failure after all safe
   cleanup actions have been attempted. Never delete a resource merely because
   its name came from the environment or configuration.
6. Preserve lifecycle verbs. A create, launch, provision, delete, or teardown
   step must perform that operation; observing a pre-existing resource does not
   satisfy it.
7. Keep TLS peer verification and SSH host-key verification enabled. A proxy,
   jump host, connection-IP override, or gateway may change the connection
   route, but must not change or disable the authenticated peer identity. Do
   not automatically trust unknown hosts.
8. Keep polling and subprocess deadlines inside the configured step timeout.
   Preserve every source-backed lifecycle threshold; runner headroom is not a
   new provider recovery threshold.
9. Emit only required structured result fields. Never include raw API bodies,
   headers, console output, stdout, stderr, or log excerpts in result JSON,
   including a general error field.
10. Honor documented semantics as well as executable assertions. Do not exploit
    a missing check or relabel a provider concept to make a schema pass.
11. Trace every result field to the source-backed concept it represents. Do not
    use placeholders, sentinels, unrelated values, or fabricated defaults.
    Respect optional fields and fail closed when required fields cannot map.
12. Report success only after actively verifying every declared success
    precondition. Credentials, paths, endpoints, and configuration values alone
    are not proof of a successful operation or connection.
13. Before setup or inventory succeeds, verify the complete resource set needed
    downstream. Keep partial readiness pending and then fail closed at the
    existing deadline. Derive counts, identities, capacities, node names,
    workload resource requests, placement, and timeouts from that same verified
    inventory and the selected checks' prerequisites, never scaffold defaults.
14. Treat provider-derived subprocess arguments as untrusted. Validate them
    against a narrow source-backed syntax or use an end-of-options boundary.
15. Use standard verified client behavior only when the reviewed interface
    establishes that flow. Do not fabricate credentials or reachability.
16. Treat an interactive console or shell as a continuing session. Establish
    readiness from evidence, terminate the probe cleanly, and do not classify
    expected continuation as failure.
17. Preserve required jump-host, proxy, or gateway topology. If the pinned
    consumer supports only a direct endpoint, return an empty change set with
    that structural blocker; never substitute the intermediary for the tested
    resource. Before retaining an existing command path or returning no
    changes, require executable preflight or empirical evidence that its
    commands are available and the route defined by the declared interfaces
    and runtime inputs reaches the target.
18. Return an empty change set only when a required interface is absent or
    structurally incompatible with the allowed contract. Otherwise implement a
    bounded, fail-closed adapter.
19. Recheck the candidate for invented inputs, operations, fields, identities,
    success claims, broadened scope, and changes outside the edit boundary.
20. Put literal, directly reviewable source code in every `change.content`.
    Never encode a candidate and reconstruct it with `compile`, `eval`, or
    `exec`.
