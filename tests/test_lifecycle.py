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

    def claim_checkpoint(self) -> str:
        return json.dumps(
            {
                "base_revision": "claim-run-correlation",
                "owned_paths": ["src/router.txt"],
                "validation": "focused lifecycle tests",
                "known_failure": "none",
                "next_safe_owner": "agent-a",
            }
        )

    def test_claim_lifecycle_is_correlated_with_one_verified_run(self) -> None:
        self.join("run-a", "agent-a")
        self.run_coord(
            "claim",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-a",
            "--task",
            "Build route A",
            "--paths",
            "src/router.txt",
        )
        claim_path = self.repo / ".agent-coordination" / "claims" / "route-a.json"
        self.assertEqual(
            json.loads(claim_path.read_text(encoding="utf-8"))["run_id"],
            "run-a",
        )
        self.run_coord(
            "update",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-a",
            "--task",
            "Finish route A",
        )
        self.run_coord(
            "pause",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-a",
            "--checkpoint",
            self.claim_checkpoint(),
            "--resume-condition",
            "Dependency is ready",
        )
        self.run_coord(
            "resume",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-a",
        )
        self.run_coord(
            "release",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-a",
            "--summary",
            "Route A completed",
        )

        claim_events = []
        for path in sorted(
            (self.repo / ".agent-coordination" / "events").glob("*.json")
        ):
            event = json.loads(path.read_text(encoding="utf-8"))
            if str(event.get("event", "")).startswith("claim-"):
                claim_events.append(event)
        self.assertEqual(
            [event["event"] for event in claim_events],
            [
                "claim-created",
                "claim-updated",
                "claim-paused",
                "claim-resumed",
                "claim-released",
            ],
        )
        self.assertTrue(
            all(event.get("run_id") == "run-a" for event in claim_events)
        )
        self.assertTrue(
            all(event.get("trace_schema") == 1 for event in claim_events)
        )

    def test_claim_auto_binds_only_when_one_owner_run_is_active(self) -> None:
        self.join("run-a", "agent-a")
        self.run_coord(
            "claim",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--task",
            "Build route A",
            "--paths",
            "src/router.txt",
        )
        claim_path = self.repo / ".agent-coordination" / "claims" / "route-a.json"
        self.assertEqual(
            json.loads(claim_path.read_text(encoding="utf-8"))["run_id"],
            "run-a",
        )

        self.run_coord(
            "release",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--summary",
            "Route A completed",
        )
        self.join("run-a-2", "agent-a")
        ambiguous = self.run_coord(
            "claim",
            "--scope",
            "route-b",
            "--owner",
            "agent-a",
            "--task",
            "Build route B",
            "--paths",
            "src/router.txt",
            expected=1,
        )
        self.assertIn("multiple active agent runs", ambiguous.stderr)

    def test_claim_rejects_a_run_owned_by_another_agent(self) -> None:
        self.join("run-b", "agent-b")
        rejected = self.run_coord(
            "claim",
            "--scope",
            "route-a",
            "--owner",
            "agent-a",
            "--run",
            "run-b",
            "--task",
            "Build route A",
            "--paths",
            "src/router.txt",
            expected=1,
        )
        self.assertIn("does not belong to", rejected.stderr)

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

    def test_work_disposition_distinguishes_waiting_from_diverted_work(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.run_coord(
            "work-suspend",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--disposition",
            "waiting",
            "--reason",
            "Agent-b owns the overlapping routing contract",
            "--contention",
            "routing-contention",
            "--blocked-by-owner",
            "agent-b",
            "--blocked-by-scope",
            "routing-refactor",
        )

        claim = json.loads(
            (
                self.repo / ".agent-coordination" / "claims" / "health.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(claim["status"], "active")
        waiting = json.loads(self.run_tx("log", "--scope", "health").stdout)[
            "events"
        ][-1]
        self.assertEqual(waiting["event"], "work-suspended")
        self.assertEqual(waiting["disposition"], "waiting")
        self.assertEqual(waiting["owner"], "agent-a")
        self.assertEqual(waiting["blocked_by_owners"], ["agent-b"])
        self.assertEqual(waiting["authority_effect"], "none")
        status = self.run_coord("status").stdout
        self.assertIn("disposition=waiting", status)
        self.assertIn("contention=routing-contention", status)

        self.run_coord(
            "work-resume",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--evidence",
            "Agent-b released the routing contract",
        )
        events = json.loads(self.run_tx("log", "--scope", "health").stdout)[
            "events"
        ]
        resumed = events[-1]
        self.assertEqual(resumed["event"], "work-resumed")
        self.assertEqual(resumed["work_state_id"], waiting["work_state_id"])
        self.assertIsInstance(resumed["suspension_duration_ms"], int)
        self.assertEqual(
            list(
                (
                    self.repo
                    / ".agent-coordination"
                    / "work"
                    / "active"
                ).glob("*.json")
            ),
            [],
        )

        self.run_coord(
            "work-suspend",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--disposition",
            "diverted",
            "--reason",
            "Continue independent documentation while routing is blocked",
            "--alternate-scope",
            "routing-docs",
        )
        diverted = json.loads(self.run_tx("log", "--scope", "health").stdout)[
            "events"
        ][-1]
        self.assertEqual(diverted["disposition"], "diverted")
        self.assertEqual(diverted["alternate_scope"], "routing-docs")

    def test_diverted_work_requires_an_explicit_alternate_task(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        rejected = self.run_coord(
            "work-suspend",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--disposition",
            "diverted",
            "--reason",
            "Do something else",
            expected=1,
        )
        self.assertIn("requires --alternate-scope or --alternate-run", rejected.stderr)
