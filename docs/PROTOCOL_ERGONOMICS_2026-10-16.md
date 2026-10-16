# Coordination ergonomics, 2026-10-16

This change keeps the ordinary Agent sequence and required arguments unchanged:

```text
join -> claim -> edit/review -> validate -> claim-finish -> leave
```

The user constraint is to reduce coordination work without adding negotiation, mandatory fields,
confirmation rounds, polling, or a new recording command. Existing exact-owner authority, dirty
baseline acknowledgement, and serialized Git publication remain in effect. The coordination
protocol remains `20260823.1` and event schema remains `2`.

## Finish an unneeded wait through the ordinary entry point

`claim-finish` can compose the existing pending-Claim release operation when that exact Owner/Run
has a pending arbitration Claim bound to its completed `wait` decision. This is an explicit
withdrawal of the caller's pending intent. It does not infer that the requested work is complete.
The caller may use it after reviewing work done by another task or deciding the scope is unneeded.

The result is `completion_kind: released-pending` and `work_result_created: false`. No source bytes,
other participant, or archived contention decision change. No write authority is granted, and no
contribution is attributed to the waiting task. The original `claim-release` path stays available.
Unresolved arbitration, pending baseline acceptance, and another Owner/Run fail closed. After a
successful finish, the same id and terminal metadata recover the archived response without another
event, including after the Run has left. Interrupted release retains the existing release/recovery
boundary; this change does not add a cross-object transaction or a new recovery state.

## Reuse existing validation context

When an inherited dirty baseline has related Work Results, its existing creation/refresh step
includes `validation_context`. The producer reads at most the three newest related results for
this context and includes at most 240 characters of matching validation text per result. This
reuses the already bounded related-result lookup; no background collector or Agent record is added.
Older results and full evidence remain available through their existing ids and records.

`declared_inputs_match` compares the exact declared paths, projection mode, and content fingerprint
against the offered baseline. Mismatches retain only their result id and negative match indicator.
Matching text is explicitly `owner_reported`; it is not proof that a test actually ran against
those bytes. The match covers declared paths at that baseline only. Dependencies outside those
paths, environment, generated resources, and artifact identity still constrain reuse. No test is
silently skipped and no integration readiness or deployment claim is inferred. Baseline acceptance
still rechecks its existing content/revision/branch identity under the operation lock.

No related result means no additional context in the ordinary output. Compact output bounds the
context and flags truncation. This metadata is diagnostic and does not grant or release authority.

## Actionable failure output

Existing stable error codes and messages remain. Known version, environment, lock, and marker
failures gain a bounded `next_action`. Version mismatch also reports the producer's supported
writable version and the workspace marker's protocol/version/state, without opening the indicated
state or changing its selector. These fields are guidance, not authority, an automatic cutover,
or a guarantee that a failed operation had no effect. Unknown errors retain the existing fallback.

## Validation scope

The regression cases cover delegated and cancelled waiting, exact Owner/Run rejection, unresolved
arbitration rejection, successful finish retries, unchanged peer bytes/authority, and follow-up
dirty-baseline acknowledgement. They also cover bounded validation excerpts, changed/wider input
scope, ignored-file projections, old records without context, and read-only version diagnostics.
The unchanged CLI finish/leave sequence is exercised as a real subprocess consumer. The complete
repository gate checks compatibility with existing producer, Observer, Console, and packaging paths.

The complete gate ran 236 tests with no failures and nine sandbox socket skips. Those exact nine
loopback/Unix-socket cases were then run with local socket access: all passed. Python compilation,
CLI entry-point checks, and whitespace checks passed. A temporary 78-file development package
passed a real finish/leave and inherited-baseline consumer check; the installed 0.2.6 producer also
accepted the new baseline metadata. The routine skill remains 129 lines. This is source validation,
not a plugin release or installed-service replacement. Source identities and development-package
evidence are in `/private/tmp/dev-mesh-ergonomics-20261016-8ov3h2yf/validation.json`; temporary evidence
may be removed by OS cleanup.
