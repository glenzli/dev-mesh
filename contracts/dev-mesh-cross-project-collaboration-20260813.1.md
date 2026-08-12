# `dev-mesh.cross-project-collaboration@20260813.1`

Status: optional extension to `dev-mesh.coordination@20260812.1`

## 1. Purpose and compatibility

This contract records an explicit collaboration that crosses Dev Mesh workspaces, such as one
Agent task requesting work from a task in another project. It provides correlation and observation;
it grants no Claim, Git, handoff, or transaction authority.

The extension does not replace or revise `dev-mesh.coordination@20260812.1`. It uses that
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

## 3. Lifecycle

The bounded lifecycle is:

```text
opened -> bound -> closed
```

- `opened` is written in the source workspace after the target task id is known. It records the
  exact active source Owner and Run and may include a known target workspace or Owner.
- `bound` is written in the target workspace by its exact active target Owner and Run. It records
  both workspace identities and copies the source correlation supplied with the request.
- `closed` may be written by either exact participant Run with outcome `completed`, `cancelled`, or
  `failed`.

The phases are observational, not authority. A missing phase is diagnosable but never blocks a Run,
Claim, Git operation, or task. The same phase in the same workspace is idempotent and must match the
original exact facts.

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
    "protocol_version": "20260813.1",
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
- Observer may aggregate matching collaboration ids across registered workspaces. It must label
  exact `bound` evidence separately from same-Run inference and must not reconstruct authority.

## 6. Operational boundary

Codex task creation, task messages, waits, and wakeups remain owned by Codex. This extension is an
explicit cooperative bridge: the initiating Agent records `opened` and includes the returned
correlation in the task communication; the receiving Agent records `bound`; one participant records
`closed`. Calls that bypass the Dev Mesh producer remain outside its observation boundary.
