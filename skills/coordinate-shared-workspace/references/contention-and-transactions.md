# Contention, Handoffs, and Microtransactions

## Contents

- Route an overlap
- Record waiting or diversion
- Coordinate a decision
- Transfer responsibility
- Use a temporary Git transaction

Load this reference only after a Claim returns `pending-arbitration`, a handoff is required, or an
explicit contention decision selects a microtransaction.

## Route an overlap

Use this order; choose the least expensive safe option:

1. Decompose scopes or semantic resources so the Claims no longer overlap.
2. Wait when the active owner will finish soon.
3. Handoff when responsibility, context, or validation should move.
4. Use `parallel-tx` only for a clean, bounded, independently testable overlap.
5. Serialize an `exclusive-refactor` that cannot be decomposed safely.

Do not create a transaction merely because two Agents exist. No overlap means no contention,
checkout, or transaction.

## Record waiting or diversion

If this Agent must stop on the overlap, preserve that fact explicitly:

```bash
python3 <skill>/scripts/coord.py --root ROOT work-suspend \
  --scope SCOPE --owner OWNER --run-id RUN \
  --disposition waiting --reason "blocked by active overlap" \
  --contention-id CONTENTION --blocked-by-owner OTHER_OWNER
```

If it can do independent work first:

```bash
python3 <skill>/scripts/coord.py --root ROOT work-suspend \
  --scope SCOPE --owner OWNER --run-id RUN \
  --disposition diverted --reason "continue independent scope first" \
  --contention-id CONTENTION --alternate-scope OTHER_SCOPE
```

Resume only with fresh evidence:

```bash
python3 <skill>/scripts/coord.py --root ROOT work-resume \
  --work-state-id WORK_STATE --owner OWNER --run-id RUN \
  --evidence "terminal decision and overlap recheck"
```

## Coordinate a decision

The initial coordinator is one participant for one contention slice, not a permanent central
Agent. All mutating calls bind the exact owner, Run, epoch, and decision revision.

The coordinator proposes one of `decompose`, `wait`, `handoff`, `parallel-tx`, or `exclusive`:

```bash
python3 <skill>/scripts/coord.py --root ROOT contention-propose \
  --contention-id CONTENTION --owner COORDINATOR --run-id COORDINATOR_RUN \
  --epoch EPOCH --decision wait --reason "active Claim will release shortly"
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

Send a handoff with a stable caller-supplied id so uncertain retries converge:

```bash
python3 <skill>/scripts/coord.py --root ROOT send \
  --source-owner OWNER --source-run-id RUN --target-owner TARGET \
  --kind handoff --topic takeover --requires-ack \
  --handoff-id HANDOFF_ID \
  --subject "bounded responsibility" --body "checkpoint and validation state"
```

The target acknowledges the returned message id with its exact active Run:

```bash
python3 <skill>/scripts/coord.py --root ROOT ack \
  --message-id MESSAGE_ID --target-owner TARGET --target-run-id TARGET_RUN \
  --note "accepted"
```

Acceptance does not transfer a Claim. Release/recreate a Claim, or use `tx-handoff` for an active
transaction. Reject or withdraw with an explicit stable reason code when the transfer will not
occur.

## Use a temporary Git transaction

Use this only after a `parallel-tx` decision for one writable Claim scope. The producer creates a
short-lived transaction branch and checkout; edit only the returned checkout.

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
