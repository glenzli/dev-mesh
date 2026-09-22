# 2026-09-20 collaboration follow-through

A read-only review of the local 00:00–15:53 Asia/Shanghai sample found 328 coordination events
across eight workspaces. All 44 events classified as cross-actor collaboration by the existing
Observer came from Mini Wiki: 32 recorded notices and six contentions with six terminal decisions.
This is a bounded historical sample, not a throughput benchmark or a claim about all model usage.

Four independent tasks agreed on a common parent-topic catalog contract, narrowed their Claims,
and routed shared metadata changes through the current writer. Five contentions were cancelled
through scope decomposition; one selected unilateral wait. The sample exposed three practical gaps.

| Observation | Correction | Boundary |
| --- | --- | --- |
| A wait decision completed in 31 seconds, but its pending Claim remained for another 16 minutes 52 seconds and later blocked a translation follow-up. | CLI status distinguishes the terminal decision from the retained Claim, blocked requests explain exact-owner cleanup, and Console shows a pending-intent diagnostic. The contention reference covers release after delegated completion. | A decision never releases a Claim automatically. The original Owner/Run must activate or release it. The full interval is not a measurement of actual stalled work. |
| Three notices used recipient aliases that did not match that day's registered Owners. | `record-message --target-run-id` derives the exact Owner, rejects a contradictory Owner before persistence, and records Run correlation in the message and immutable events. | Legacy Owner-only recording remains supported and explicitly reports whether that Owner is registered; it never silently selects a Run. |
| A published catalog entry preceded its required entry point, locale, and learning files, temporarily breaking peer-wide validation. | The routine skill now requires integration dependencies to be ready before enabling a shared catalog/build entry and requires unchanged inputs plus an identified artifact for validation reuse. | This is workflow guidance. Dev Mesh does not interpret project catalogs or guarantee that a shared worktree is continuously buildable. |

## Runtime and recording changes

- `contention-wait` output reports `decision_releases_claim: false` and the cleanup action for
  delegated or unnecessary work. This describes the decision's effect, including on retries; it
  does not claim to know whether a later operation has already released the Claim.
- `status` correlates an outstanding pending Claim with its exact archived wait decision and
  returns `activate_after_overlap_release_or_release_if_work_delegated`. The projection does not
  rewrite Claims, refresh heartbeats, or grant authority.
- Observer emits the informational `claim.pending-after-wait` diagnostic while the matching Claim
  remains pending. Console labels it as pending in both languages; it disappears after activation
  or release. The historical contention remains completed.
- Optional `target_run_id` and `target_task_id` fields retain recipient correlation. The Run is
  validated against workspace state; the opaque host task id is caller-provided evidence and is
  not checked against the host or treated as a delivery receipt.
- An explicitly bound recipient Run must be the one acknowledging or rejecting that message.
  Another Run with the same Owner cannot substitute. Unbound legacy messages keep their behavior.
- Stable handoff retries compare the optional correlation fields and recover the existing
  message/offer/events; they cannot silently rebind a previously recorded offer.
- Exact same-Owner, different-Run message events now contain both Run identities, allowing the
  existing collaboration classifier to recognize the relation.

The producer and event protocol remain `20260823.1` and schema `2`. These are optional evidence and
presentation additions. Existing event names, legacy message calls, Claim state transitions,
managed Git boundaries, and explicit authority transfer requirements remain unchanged. Existing
historical messages are not rewritten. No recovery or release was performed on another task's state.

## Regression cases and validation

The repository tests exercise:

1. A writer incorporates a pending participant's request, finishes its own Claim, and is blocked
   by the retained intent. Status remains read-only. Only the pending participant can release it;
   the follow-up writer must still accept the inherited dirty baseline.
2. A contradictory Owner/Run pair fails without persisting a message. An exact Run derives the
   correct Owner and retains task correlation. An unknown legacy Owner remains visibly unbound.
3. A same-Owner sibling cannot acknowledge or reject a bound handoff; accepted handoff evidence
   does not transfer the source Claim.
4. An interrupted bound handoff retry recovers one offer/event and rejects changed task correlation.
5. Observer shows a pending intent even when active contention count is zero, then removes the
   diagnostic after the Claim is resolved.

Focused commands:

```bash
PYTHONPATH=runtime:runtime/tests python3 -m unittest test_collaboration test_cli_output test_cli -v
PYTHONPATH=runtime:runtime/tests python3 -m unittest test_observer_diagnostics test_console_runtime -v
```

The first focused pass ran 38 tests successfully. The Observer/Console pass ran 37 tests with
three existing loopback-environment skips and no failures. The full repository suite ran 220 tests
with eight environment-dependent skips and no failures, covering producer, recovery, Observer,
Console, packaging, and compatibility behavior.

A subsequent live Console check exposed one remaining presentation inconsistency: its main
guidance still described the contention as entirely finished while the pending Claim remained.
The final UI change now distinguishes the completed decision from the outstanding Claim. After
that change, the focused Observer/Console suite ran 12 tests with three environment-dependent
skips and no failures. JavaScript syntax checks, Python compilation, skill validation, and
`git diff --check` passed. The unchanged full suite was not repeated for this presentation change.

The source Console was also run on loopback against an isolated temporary Git workspace and
Observer database. Browser checks confirmed the pending guidance and diagnostic in both English
and Chinese, with no browser warnings or errors. After the fixture's original participants
released their Claims and closed their Runs, a refresh removed the pending guidance and diagnostic
while preserving the completed contention in history. This verifies the temporary source instance,
not the installed service.

Test logs and the source-identity evidence manifest are retained in
`/private/tmp/dev-mesh-analysis-20260920/` and linked from the managed Work Result. That temporary
evidence can be removed by OS cleanup; this document is the durable repository record.

## Scope of delivery

The repair is a local source change and is recorded as a managed Work Result. It does not publish a
plugin release, replace an installed cache, commit inherited Discovery changes, or deploy Mini Wiki.
Runtime tests verify temporary workspaces and the source under review; they do not imply that an
already running installed Console has loaded these changes.
