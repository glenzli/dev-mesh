# Contention, Handoffs, and Microtransactions

## Contents

- Route an overlap
- Record waiting or diversion
- Coordinate a decision
- Transfer responsibility
- Use a temporary Git transaction

Load this reference only after a Claim returns `pending-arbitration`, a handoff is required, or an
explicit contention decision selects a microtransaction.

`workspace-bytes` Claims never enter a Git microtransaction. The coordinator rejects a
`parallel-tx` proposal before recording the decision. Existing ignored data is not present
in a linked worktree, so the pending writer selects wait, receives the released byte baseline, and
continues in the shared workspace. Handoff or exclusive retention remain available when waiting is
not appropriate.

## Route an overlap

Use this order; choose the least expensive safe option:

1. Decompose this Agent's own scope or semantic resources so a replacement Claim no longer
   overlaps. This is a local release-and-reclaim action, not a shared contention decision.
2. Wait when the active owner will finish soon.
3. Handoff when responsibility, context, or validation should move.
4. Use `parallel-tx` only for a clean, bounded, independently testable overlap.
5. Serialize an `exclusive-refactor` that cannot be decomposed safely.

Do not create a transaction merely because two Agents exist. No overlap means no contention,
checkout, or transaction.

To decompose after a pending Claim already opened contention, end only that pending intent and then
declare the narrower scope; do not ask the active owner to approve your smaller scope:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-cancel \
  --contention-id CONTENTION --scope SCOPE --owner OWNER --run-id RUN \
  --reason-code scope-decomposed --reason "retry with a non-overlapping scope"

python3 <skill>/scripts/coord.py --root ROOT claim-release \
  --scope SCOPE --owner OWNER --run-id RUN --summary "replace broad pending intent"

python3 <skill>/scripts/coord.py --root ROOT claim \
  --scope NEW_SCOPE --owner OWNER --run-id RUN --task "narrowed work" \
  --path NON_OVERLAPPING_PATH --intent local-edit
```

## Select simple waiting or record diversion

The pending trigger Claim may wait unilaterally. Waiting grants no write authority and does not
change the active Claim, so it needs no proposal, response, enact, or extra suspended-work record:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-wait \
  --scope SCOPE --owner OWNER --run-id RUN \
  --contention-id CONTENTION --reason "active edit should finish shortly"
```

After the active Claim releases or completes, recheck under the operation lock. No free-form
evidence is required for this exact wait decision:

```bash
python3 <skill>/scripts/coord.py --root ROOT claim-activate \
  --scope SCOPE --owner OWNER --run-id RUN
```

If the active editor left dirty work, activation returns `pending-baseline`; inspect it and accept
the returned `accept_baseline_sha256`. If the content, canonical revision, or branch changes before
acceptance, the same accept call refreshes the pending Claim and returns current evidence instead
of stranding the workflow; inspect it and retry once.

Use suspended work only if the Agent actually diverts to independent work and the interruption is
valuable to observe:

```bash
python3 <skill>/scripts/coord.py --root ROOT work-suspend \
  --scope SCOPE --owner OWNER --run-id RUN \
  --disposition diverted --reason "continue independent scope first" \
  --contention-id CONTENTION --alternate-scope OTHER_SCOPE
```

Resume diverted work only with fresh evidence:

```bash
python3 <skill>/scripts/coord.py --root ROOT work-resume \
  --work-state-id WORK_STATE --owner OWNER --run-id RUN \
  --evidence "terminal decision and overlap recheck"
```

## Coordinate a decision

The initial coordinator is one participant for one contention slice, not a permanent central
Agent. All mutating calls bind the exact owner, Run, epoch, and decision revision.

The full shared decision path is reserved for `handoff`, `parallel-tx`, or `exclusive`. The wire
name `parallel-tx` means branch offload: the pending Claim receives one short transaction checkout
while the established Claim remains on the canonical workspace. It is not two symmetric branches.
Both Claims must declare nonempty semantic writes, and those resources must pass the existing
independence check; missing semantic evidence fails closed.

The coordinator proposes one of those shared decisions:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-propose \
  --contention-id CONTENTION --owner COORDINATOR --run-id COORDINATOR_RUN \
  --epoch EPOCH --decision parallel-tx --reason "bounded independent branch offload"
```

Each participant accepts or rejects that exact revision:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-respond \
  --contention-id CONTENTION --scope SCOPE --owner OWNER --run-id RUN \
  --revision REVISION --accept --reason "accepted"
```

After every participant accepts, the exact coordinator enacts:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-enact \
  --contention-id CONTENTION --owner COORDINATOR --run-id COORDINATOR_RUN \
  --epoch EPOCH
```

If the coordinator stops responding, another participant may acquire only after the recorded lease
expires, using the exact expected epoch. This transfers the coordination role, not any Claim.

## Transfer responsibility

First use the current environment's actual task, team, thread, or subagent communication control to
deliver the handoff to the real target task. If the target task does not exist, create or dispatch it
before continuing. If delivery fails, stop: do not write a Dev Mesh handoff and do not describe the
target as notified.

After actual delivery succeeds, record the handoff with a stable caller-supplied id so uncertain
recording retries converge:

```bash
python3 <skill>/scripts/coord.py --root ROOT send \
  --source-owner OWNER --source-run-id RUN --target-owner TARGET \
  --kind handoff --topic takeover --requires-ack \
  --handoff-id HANDOFF_ID \
  --subject "bounded responsibility" --body "checkpoint and validation state"
```

The sender must provide the returned message id to the target through the actual communication
channel. The target then explicitly records acknowledgement with its exact active Run:

```bash
python3 <skill>/scripts/coord.py --root ROOT ack \
  --message-id MESSAGE_ID --target-owner TARGET --target-run-id TARGET_RUN \
  --note "accepted"
```

`send` success proves only that the offer was recorded; it does not prove delivery and it does not
run `ack` for the receiver. Recorded acceptance does not transfer a Claim. For clean direct work,
release the Claim and let the accepted target create its own exact Claim. For completed dirty
direct work, create a Work Result and leave; the target then creates its own Claim and explicitly
accepts the inherited dirty-baseline digest before editing. Use
`tx-handoff` for an active transaction. Reject or withdraw with an explicit stable reason code when
the responsibility transfer will not occur.

## Use a temporary Git transaction

Use this only after a `parallel-tx` branch-offload decision for the pending writable Claim. The
producer creates one short-lived transaction branch and checkout for that Claim; the established
owner stays on the canonical workspace. Edit only the returned checkout.

```bash
python3 <skill>/scripts/coord.py --root ROOT tx-begin \
  --scope SCOPE --owner OWNER --run-id RUN \
  --contention-id CONTENTION --reason "bounded independent overlap"
```

Prepare one commit and record its exact paths:

```bash
python3 <skill>/scripts/coord.py --root ROOT tx-prepare \
  --transaction-id TX --owner OWNER --owner-run-id RUN \
  --summary "bounded candidate"
```

Validate the exact candidate:

```bash
python3 <skill>/scripts/coord.py --root ROOT tx-validate \
  --transaction-id TX --owner OWNER --owner-run-id RUN \
  --evidence "focused tests passed"
```

The active steward serializes publication:

```bash
python3 <skill>/scripts/coord.py --root ROOT tx-publish \
  --transaction-id TX --steward STEWARD --steward-run-id STEWARD_RUN
```

Publication is fast-forward only. A canonical advance may refresh the candidate to `prepared`; it
must then be revalidated. For ownership transfer use `tx-handoff` with both exact Runs and a
checkpoint. For abandonment use `tx-abort`; destructive cleanup requires explicit `--discard` or a
fresh `tx-cleanup-authorize` revision. Never resolve transaction conflicts in the canonical
workspace.

If any transaction call reports `needs-attention`, stop mutation, rerun it with root `--verbose`,
and load the recovery reference.
