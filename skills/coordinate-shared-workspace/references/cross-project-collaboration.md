# Cross-project collaboration

Load this reference when the current task creates, messages, waits on, or hands work to a Codex task
whose development workspace differs from the current Git workspace. This is correlation only; it
does not grant authority in either workspace.

First actually call the available Codex task control to create, identify, or message the real target
task. Describing that action, writing a Dev Mesh message, or recording `opened` does not execute it.
Dev Mesh cannot deliver a message to a Codex task or wake it, and `target_owner` is not a task
address. Once the task id is known, record `opened`, then send the returned correlation to that task
through the actual Codex task control. If creation already dispatched an initial prompt, send the
correlation as a follow-up.
A workspace-local request or handoff may accompany the work after both Runs exist, but it does not
establish a cross-project relation. Identical Owner/Run text in multiple workspaces is only a
possible sign that one task visited several projects.

## Open the relation

Join an exact Run in the source workspace first. After the real task communication provides the
target task id, choose one stable collaboration id and record the source edge:

```bash
python3 <skill>/scripts/coord.py --root SOURCE_ROOT cross-project-open \
  --collaboration-id RELATION_ID \
  --source-owner SOURCE_OWNER --source-run-id SOURCE_RUN \
  --target-task-id TARGET_TASK_ID --kind request
```

Add `--target-workspace-id` or `--target-owner` only when already known. Never infer either value.
The result returns `collaboration_id`, `source_workspace_id`, source Owner/Run, and target task id.
Include those exact fields in the Codex task message so the receiver can bind the relation. Do not
copy prompt text, tool output, or source paths into Dev Mesh.

## Bind in the target workspace

The receiving task joins its own exact Run and records:

```bash
python3 <skill>/scripts/coord.py --root TARGET_ROOT cross-project-bind \
  --collaboration-id RELATION_ID \
  --source-workspace-id SOURCE_WORKSPACE_ID \
  --source-owner SOURCE_OWNER --source-run-id SOURCE_RUN \
  --target-owner TARGET_OWNER --target-run-id TARGET_RUN \
  --target-task-id TARGET_TASK_ID --kind request
```

The kind must exactly match the source record. Binding does not acknowledge a workspace-local
request or transfer a Claim; use the normal message/handoff lifecycle separately when those
semantics are needed.

## Close once

After the requested cross-project work reaches a terminal result, one exact participant records
`completed`, `cancelled`, or `failed`. Close the relation **before** that participant leaves its Run;
`leave` does not close cross-project evidence automatically. This example closes from the target:

```bash
python3 <skill>/scripts/coord.py --root TARGET_ROOT cross-project-close \
  --collaboration-id RELATION_ID --actor-role target \
  --owner TARGET_OWNER --run-id TARGET_RUN \
  --source-workspace-id SOURCE_WORKSPACE_ID \
  --source-owner SOURCE_OWNER --source-run-id SOURCE_RUN \
  --target-workspace-id TARGET_WORKSPACE_ID \
  --target-owner TARGET_OWNER --target-run-id TARGET_RUN \
  --target-task-id TARGET_TASK_ID --kind request --outcome completed
```

Retry an uncertain phase with exactly the same facts. A retry repairs missing immutable evidence and
does not append a duplicate event. Normal open/bind/close produces three small events total; do not
record heartbeats, every chat message, or every wait poll.

Do not decide that a relation is unbound from the source workspace's `opened` record alone. The
receiver's `bound` evidence lives in the target workspace. Query the multi-workspace Observer or ask
the target task for its exact correlation state before requesting another bind.

### Reconcile a missed close

If the exact bound target Run already terminated before recording `closed`, do not rejoin it, recover
it, or bind a replacement Run over the immutable target identity. A new active Run of the **same
target Owner** may close from the target workspace using the existing bound record:

```bash
python3 <skill>/scripts/coord.py --root TARGET_ROOT cross-project-reconcile-close \
  --collaboration-id RELATION_ID \
  --owner TARGET_OWNER --run-id ACTIVE_SUCCESSOR_RUN \
  --outcome completed
```

This operation requires the original bound target Run to be terminal. It preserves both original
participants and records the active successor separately as `reconciliation.by`. It grants no
authority and exists only to make the observational lifecycle converge after a missed close.

Supported kinds are `notice`, `request`, `dependency`, `handoff`, `review`, and `integration`.
Choose the narrowest semantic kind and keep it unchanged through the relation.
