# Cross-project collaboration

Load this reference when the current task creates, messages, waits on, or hands work to a Codex task
whose development workspace differs from the current Git workspace. This is correlation only; it
does not grant authority in either workspace.

## Open the relation

Join an exact Run in the source workspace first. After Codex provides the target task id, choose one
stable collaboration id and record the source edge:

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
`completed`, `cancelled`, or `failed`. This example closes from the target:

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

Supported kinds are `notice`, `request`, `dependency`, `handoff`, `review`, and `integration`.
Choose the narrowest semantic kind and keep it unchanged through the relation.
