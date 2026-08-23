# `dev-mesh.cross-project-collaboration@20260823.1`

Status: optional extension to `dev-mesh.coordination@20260823.1`

## 1. Purpose and compatibility

This contract records an explicit collaboration that crosses Dev Mesh workspaces, such as one
Agent task requesting work from a task in another project. It provides correlation and observation;
it grants no Claim, Git, handoff, or transaction authority.

The extension does not replace or revise `dev-mesh.coordination@20260823.1`. It uses that
protocol's existing `message-sent` event as a carrier and adds one `cross_project` payload. The base
event schema permits additional fields, so an older producer or Observer can ignore this extension
without migrating or resetting `.dev-mesh/` state.

## 2. Identity

One collaboration has a caller-supplied stable `collaboration_id`. Every participating workspace is
identified by:

```text
first 24 lowercase hexadecimal characters of
SHA-256(UTF-8(canonical resolved Git workspace root))
```

The identifier is intentionally the same non-secret workspace identity used by the Dev Mesh
Observer. A project path is never copied into an event.

The source records the target Codex task id when it is known. The target workspace, Owner, and Run
may be unknown at that point. A receiver binds its exact active Run later; producers never infer a
missing target from timestamps, similar names, or conversation text.

Owner and Run identities are scoped by workspace. Identical Owner/Run strings in two workspaces do
not establish a collaboration: they may represent one Codex task visiting multiple projects or
unrelated local naming. Observer may expose this only as explicitly labelled, non-directional hint
evidence.

## 3. Lifecycle

The bounded lifecycle is:

```text
opened -> bound -> closed
```

- `opened` is written in the source workspace after the target task id is known. It records the
  exact active source Owner and Run and may include a known target workspace or Owner.
- `bound` is written in the target workspace by its exact active target Owner and Run. It records
  both workspace identities and copies the source correlation supplied with the request.
- `closed` is normally written by either exact participant Run with outcome `completed`,
  `cancelled`, or `failed`, before that Run leaves.
- If the bound target Run already terminated without `closed`, one active successor Run of the same
  target Owner may reconcile the close from the target workspace. The original source and target
  identities remain immutable; the successor is recorded separately as `reconciliation.by`.

The phases are observational, not authority. A missing phase is diagnosable but never blocks a Run,
Claim, Git operation, or task. The same phase in the same workspace is idempotent and must match the
original exact facts.

The additive reconciliation form is compatible with this extension version: it introduces no new
authority effect and preserves the existing phase, participant, kind, and outcome fields. Consumers
that do not understand `reconciliation` may continue to treat the event as an ordinary `closed`
phase.

Collaboration kinds are bounded to:

```text
notice | request | dependency | handoff | review | integration
```

## 4. Carrier payload

Each phase appends one base `message-sent` event whose `authority_effect` remains `none`. Its
additional payload has this form:

```json
{
  "cross_project": {
    "protocol": "dev-mesh.cross-project-collaboration",
    "protocol_version": "20260823.1",
    "collaboration_id": "echo-infer-review-20260813",
    "phase": "bound",
    "kind": "review",
    "actor_role": "target",
    "source": {
      "workspace_id": "0123456789abcdef01234567",
      "owner": "echo-agent",
      "run_id": "echo-review-run"
    },
    "target": {
      "workspace_id": "89abcdef0123456789abcdef",
      "task_id": "019ff22e-04d2-7b33-b603-53a9c0ca4d63",
      "owner": "infer-agent",
      "run_id": "infer-review-run"
    },
    "outcome": null
  }
}
```

A reconciled target-side close additionally carries bounded factual evidence:

```json
{
  "reconciliation": {
    "basis": "bound-target-run-terminal",
    "by": {
      "workspace_id": "89abcdef0123456789abcdef",
      "owner": "infer-agent",
      "run_id": "infer-review-close-run"
    },
    "target_run_status": "closed",
    "target_run_outcome": "completed"
  }
}
```

The producer derives the original participants from the target workspace's exact bound record. It
must reject a different Owner, an active original target Run, a missing or malformed bound record,
or any attempt to change the original workspace, task, Owner, Run, or kind.

The producer persists a metadata-only inert message snapshot containing a prebuilt exact event,
then appends that event. A retry repairs an event-append gap using the same event id; it does not
scan or rewrite prior event history.

## 5. Privacy and bounds

- No prompt, message body, tool output, token count, file content, diff, or Agent reasoning is
  recorded.
- Identifiers use the base protocol's bounded identifier grammar. Workspace ids are exactly 24
  lowercase hexadecimal characters.
- One normal collaboration produces three small immutable events. An implementation must not emit
  heartbeats or per-tool-call events for this extension.
- A reconciled collaboration still produces exactly one `closed` event; it does not add a separate
  recovery event.
- Observer may aggregate matching collaboration ids across registered workspaces. Only exact
  receiver `bound` evidence, or a later exact `closed` phase that repeats both participants, counts
  as cross-task collaboration. Same-Run names may be shown as a hint but must not contribute to
  collaboration counts or direction, and workspace-local messages or handoffs must not be promoted
  into cross-project evidence.

## 6. Operational boundary

Codex task creation, task messages, waits, and wakeups remain owned by Codex. Base-protocol `send`,
acknowledgement, and handoff are passive workspace-local records; they never deliver to, start,
resume, or wake a Codex task. This extension is an explicit cooperative bridge: the initiating
Agent creates or identifies the real target task, records `opened` after its task id is known, and
uses the actual Codex task control to deliver the returned correlation. When task creation already
dispatched an initial prompt, that delivery is a follow-up message. The receiving Agent records
`bound`; one participant records `closed`. Calls that bypass the Dev Mesh producer remain outside
its observation boundary.

Source and target evidence is distributed. A source-local `opened` record with a null target Run is
not proof that the target remains unbound; multi-workspace observation or target-side evidence is
required before requesting another bind. If a target misses normal closure, target-side
same-owner reconciliation closes the diagnostic relation without reviving or replacing the
completed participant Run.
