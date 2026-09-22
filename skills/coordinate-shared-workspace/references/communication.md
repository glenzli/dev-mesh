# Execute communication, then record it

Load for task messages and handoffs. Dev Mesh records the action; the host task tool performs it.

1. Identify the real target task and use the host's task, team, or subagent tool to send the notice,
   request, or handoff. Create or resume a task only within the user's authorized scope.
2. If actual delivery fails or is unavailable, report that the target was not contacted. Do not
   write a successful communication record or claim notification occurred.
3. Match the recipient's registered Run from scoped status to the real task. Do not turn its title,
   nickname, or a remembered alias into an Owner. After delivery succeeds, persist the exact identity:

```bash
python3 <skill>/scripts/coord.py --root ROOT record-message \
  --source-owner OWNER --source-run-id RUN --target-run-id TARGET_RUN \
  --target-task-id HOST_TASK_ID \
  --kind notice --subject "bounded update" --body "delivered checkpoint"
```

`record-message` and the legacy `send` command execute the same passive record operation. Neither
starts, resumes, delivers to, or wakes a task. `target-owner` is an authority identity, not a task
address. A notice returns `recording_complete`; no second delivery is needed.

`target-run-id` derives the registered Owner; supplying a mismatched `target-owner` fails before
recording. The optional host task id is correlation supplied by the caller, not verified delivery.
An exact target Run also binds acknowledgement or rejection to that Run. Do not bind an unrelated
Run merely because its Owner matches. Recording can refer to an already closed Run when the actual
delivery preceded its completion; no authority is granted by recording.

When the target has not joined this workspace, the compatible `--target-owner TARGET` form remains
available. Its output explicitly reports `owner-only` or `unregistered-owner` and bounded matching
Runs. It never guesses an exact Run. Use the cross-project workflow for another workspace.

For a decision request use `--kind request --requires-ack`. The returned action is
`share_message_id_then_wait_for_acknowledgement`: provide only the correlation and acknowledgement
instruction through the actual task tool, without resending the original work request. The receiver
runs `ack` with its exact active Owner/Run. Dev Mesh does not poll or contact it.

For a handoff, load [contention-and-transactions.md](contention-and-transactions.md). Use a stable
caller-supplied `--handoff-id` and retry with that id after an uncertain recording result. Acceptance
never transfers a Claim implicitly: finish dirty direct work with a Work Result, then let the target
Claim and accept the inherited baseline. Active transactions use `tx-handoff`.

For cross-project work, load [cross-project-collaboration.md](cross-project-collaboration.md). One
relation normally records the work's open/bind/close; do not also record every poll or repeat those
phases as notices. A separate workspace-local request or handoff is useful only when it has its own
decision or authority lifecycle.
