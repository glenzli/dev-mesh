from __future__ import annotations

import json
from pathlib import Path

from tests.integration_support import TransactionRepositoryCase


class AgentLifecycleIntegrationTest(TransactionRepositoryCase):
    def join(
        self,
        run_id: str,
        owner: str,
        *,
        parent_owner: str | None = None,
    ) -> None:
        arguments = [
            "agent-join",
            "--run",
            run_id,
            "--owner",
            owner,
            "--task",
            f"Work assigned to {owner}",
        ]
        if parent_owner is not None:
            arguments.extend(("--parent-owner", parent_owner))
        self.run_coord(*arguments)

    def offer_handoff(self, handoff_id: str = "router-handoff") -> str:
        message = self.run_coord(
            "message",
            "--to",
            "agent-b",
            "--from-owner",
            "agent-a",
            "--subject",
            "Continue the router slice",
            "--body",
            "Checkpoint is validated; continue from the current canonical revision.",
            "--type",
            "handoff",
            "--requires-ack",
            "--run",
            "run-a",
            "--handoff",
            handoff_id,
        )
        return Path(message.stdout.strip()).stem

    def test_agent_runs_and_handoff_form_a_queryable_chain(self) -> None:
        self.join("run-a", "agent-a")
        self.join("run-b", "agent-b", parent_owner="agent-a")
        message_id = self.offer_handoff()
        self.run_coord(
            "ack",
            "--owner",
            "agent-b",
            "--message-id",
            message_id,
            "--run",
            "run-b",
            "--note",
            "Checkpoint and ownership boundary reviewed",
        )
        self.run_coord(
            "agent-leave",
            "--run",
            "run-a",
            "--owner",
            "agent-a",
            "--outcome",
            "completed",
            "--summary",
            "Handoff accepted by agent-b",
        )
        self.run_coord(
            "agent-leave",
            "--run",
            "run-b",
            "--owner",
            "agent-b",
            "--outcome",
            "completed",
            "--summary",
            "Router slice completed",
        )

        source_log = json.loads(self.run_tx("log", "--run", "run-a").stdout)
        self.assertEqual(
            [event["event"] for event in source_log["events"]],
            ["agent-joined", "message-sent", "handoff-offered", "handoff-accepted", "agent-left"],
        )
        handoff_log = json.loads(
            self.run_tx("log", "--handoff", "router-handoff").stdout
        )
        self.assertEqual(
            [event["event"] for event in handoff_log["events"]],
            ["message-sent", "handoff-offered", "message-acknowledged", "handoff-accepted"],
        )
        accepted = next(
            event for event in handoff_log["events"] if event["event"] == "handoff-accepted"
        )
        self.assertEqual(accepted["source_run_id"], "run-a")
        self.assertEqual(accepted["target_run_id"], "run-b")

        coverage = json.loads(self.run_coord("coverage").stdout)
        self.assertEqual(
            coverage["summary"],
            {
                "runs_joined": 2,
                "runs_closed": 2,
                "runs_open": 0,
                "handoffs_offered": 1,
                "handoffs_accepted": 1,
                "handoffs_pending": 0,
                "issue_count": 0,
            },
        )

    def test_coverage_exposes_open_runs_and_pending_handoffs(self) -> None:
        self.join("run-a", "agent-a")
        self.offer_handoff("pending-router-handoff")

        coverage = json.loads(self.run_coord("coverage").stdout)
        self.assertEqual(coverage["summary"]["runs_open"], 1)
        self.assertEqual(coverage["summary"]["handoffs_pending"], 1)
        self.assertEqual(coverage["open_runs"][0]["run_id"], "run-a")
        self.assertEqual(
            coverage["pending_handoffs"][0]["handoff_id"],
            "pending-router-handoff",
        )

    def test_handoff_requires_joined_runs_and_explicit_acknowledgement(self) -> None:
        self.join("run-a", "agent-a")
        missing_ack = self.run_coord(
            "message",
            "--to",
            "agent-b",
            "--from-owner",
            "agent-a",
            "--subject",
            "Unsafe handoff",
            "--body",
            "This must not become an unacknowledged transfer.",
            "--type",
            "handoff",
            "--run",
            "run-a",
            expected=1,
        )
        self.assertIn("require --requires-ack", missing_ack.stderr)

        message_id = self.offer_handoff()
        missing_target_run = self.run_coord(
            "ack",
            "--owner",
            "agent-b",
            "--message-id",
            message_id,
            "--run",
            "run-b",
            expected=1,
        )
        self.assertIn("agent run is not joined", missing_target_run.stderr)

    def test_handoff_uses_its_message_id_when_no_explicit_id_is_given(self) -> None:
        self.join("run-a", "agent-a")
        self.join("run-b", "agent-b")
        message = self.run_coord(
            "message",
            "--to",
            "agent-b",
            "--from-owner",
            "agent-a",
            "--subject",
            "Continue without a caller-generated id",
            "--body",
            "The protocol should use the durable message id.",
            "--type",
            "handoff",
            "--requires-ack",
            "--run",
            "run-a",
        )
        message_id = Path(message.stdout.strip()).stem
        self.run_coord(
            "ack",
            "--owner",
            "agent-b",
            "--message-id",
            message_id,
            "--run",
            "run-b",
        )

        handoff_log = json.loads(
            self.run_tx("log", "--handoff", message_id).stdout
        )
        self.assertEqual(handoff_log["filters"]["handoff_id"], message_id)
        self.assertEqual(
            {event["event"] for event in handoff_log["events"]},
            {
                "message-sent",
                "handoff-offered",
                "message-acknowledged",
                "handoff-accepted",
            },
        )

    def test_join_and_leave_retries_are_idempotent(self) -> None:
        self.join("run-a", "agent-a")
        self.join("run-a", "agent-a")
        for _ in range(2):
            self.run_coord(
                "agent-leave",
                "--run",
                "run-a",
                "--owner",
                "agent-a",
                "--outcome",
                "completed",
                "--summary",
                "Work completed",
            )
        coverage = json.loads(self.run_coord("coverage").stdout)
        self.assertEqual(coverage["summary"]["runs_joined"], 1)
        self.assertEqual(coverage["summary"]["runs_closed"], 1)
        self.assertEqual(coverage["summary"]["issue_count"], 0)
