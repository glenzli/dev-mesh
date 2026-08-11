from __future__ import annotations

import json
import unittest

from tests.integration_support import TransactionRepositoryCase


class SchedulingIntegrationTest(TransactionRepositoryCase):
    def custom_claim(
        self,
        scope: str,
        owner: str,
        path: str,
        semantic_write: str,
        *,
        intent: str = "additive",
        depends_on: tuple[str, ...] = (),
        pending: bool = True,
        expected: int = 0,
    ):
        arguments = [
            "claim",
            "--scope",
            scope,
            "--owner",
            owner,
            "--task",
            f"Implement {scope}",
            "--paths",
            path,
            "--intent",
            intent,
            "--semantic-writes",
            semantic_write,
            "--first-release",
            f"{scope} is complete",
            "--allow-overlap",
            "--reason",
            "Queue this request behind existing overlapping work",
        ]
        if depends_on:
            arguments.extend(("--depends-on", *depends_on))
        if pending:
            arguments.append("--pending-on-conflict")
        return self.run_coord(*arguments, expected=expected)

    def enqueue(self, scopes: tuple[str, ...], mode: str) -> dict[str, object]:
        return json.loads(
            self.run_tx(
                "enqueue",
                "--scopes",
                *scopes,
                "--mode",
                mode,
                "--steward",
                "central",
                "--reason",
                f"Schedule {mode} without bypassing older overlap",
            ).stdout
        )

    def active_requests(self) -> list[dict[str, object]]:
        status = json.loads(self.run_tx("status", "--json").stdout)
        return status["requests"]

    def test_enqueued_transaction_activates_and_manual_begin_cannot_bypass_it(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        request = self.enqueue(("health", "metrics"), "parallel-tx")

        rejected = self.run_tx(
            "begin",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            "parallel-tx",
            "--steward",
            "central",
            "--reason",
            "Attempt to bypass queue",
            expected=1,
        )
        self.assertIn("would bypass queued request", rejected.stderr)

        updates = json.loads(self.run_tx("schedule", "--steward", "central").stdout)

        self.assertTrue(
            any(
                update.get("request_id") == request["request_id"]
                and update.get("action") == "activated"
                for update in updates
            )
        )
        self.assertEqual(self.active_requests(), [])
        transactions = json.loads(self.run_tx("status", "--json").stdout)[
            "transactions"
        ]
        self.assertEqual(len(transactions), 2)
        self.assertTrue(all(transaction["status"] == "active" for transaction in transactions))

    def test_older_exclusive_request_blocks_later_overlap_but_not_disjoint_work(self) -> None:
        self.custom_claim("health", "agent-a", "src/router.txt", "route:/health")
        self.custom_claim(
            "refactor",
            "agent-r",
            "src/router.txt",
            "contract:routing",
            intent="refactor",
        )
        exclusive = self.enqueue(("refactor",), "exclusive")

        self.custom_claim("notes-a", "agent-c", "notes.txt", "note:a")
        self.custom_claim("notes-b", "agent-d", "notes.txt", "note:b")
        disjoint = self.enqueue(("notes-a", "notes-b"), "parallel-tx")

        updates = json.loads(self.run_tx("schedule", "--steward", "central").stdout)

        self.assertTrue(
            any(
                update.get("request_id") == disjoint["request_id"]
                and update.get("action") == "activated"
                for update in updates
            )
        )
        remaining = self.active_requests()
        self.assertEqual([request["request_id"] for request in remaining], [exclusive["request_id"]])
        self.assertEqual(remaining[0]["status"], "blocked")
        self.assertIn("active claim health", remaining[0]["blockers"])
        log = json.loads(
            self.run_tx("log", "--request", str(exclusive["request_id"])).stdout
        )["events"]
        requested = next(event for event in log if event["event"] == "queue-requested")
        blocked = next(event for event in log if event["event"] == "queue-blocked")
        self.assertEqual(requested["trace_schema"], 1)
        self.assertEqual(requested["owners"], ["agent-r"])
        self.assertEqual(requested["scopes"], ["refactor"])
        self.assertIn(
            {
                "kind": "claim",
                "scope": "health",
                "owner": "agent-a",
            },
            blocked["blocker_refs"],
        )

    def test_exclusive_request_prevents_later_optimistic_starvation(self) -> None:
        self.custom_claim("health", "agent-a", "src/router.txt", "route:/health")
        self.custom_claim(
            "refactor",
            "agent-r",
            "src/router.txt",
            "contract:routing",
            intent="refactor",
        )
        exclusive = self.enqueue(("refactor",), "exclusive")
        self.custom_claim("metrics", "agent-b", "src/router.txt", "route:/metrics")
        self.custom_claim("tracing", "agent-c", "src/router.txt", "route:/tracing")
        optimistic = self.enqueue(("metrics", "tracing"), "parallel-tx")

        self.run_tx("schedule", "--steward", "central")
        requests = {request["request_id"]: request for request in self.active_requests()}
        self.assertIn(
            f"earlier overlapping request {exclusive['request_id']}",
            requests[optimistic["request_id"]]["blockers"],
        )

        late = self.custom_claim(
            "late-route",
            "agent-late",
            "src/router.txt",
            "route:/late",
            pending=False,
            expected=1,
        )
        self.assertIn("older exclusive request blocks", late.stderr)

        self.run_coord(
            "release",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--summary",
            "Health slice completed",
        )
        self.run_tx("schedule", "--steward", "central")
        refactor_path = self.repo / ".agent-coordination" / "claims" / "refactor.json"
        refactor = json.loads(refactor_path.read_text(encoding="utf-8"))
        self.assertEqual(refactor["status"], "active")
        self.assertEqual(refactor["exclusive_request_id"], exclusive["request_id"])
        requests = self.active_requests()
        self.assertEqual([request["request_id"] for request in requests], [optimistic["request_id"]])
        self.assertIn("active claim refactor", requests[0]["blockers"])

        self.run_coord(
            "release",
            "--scope",
            "refactor",
            "--owner",
            "agent-r",
            "--summary",
            "Exclusive refactor completed",
        )
        completed = json.loads(self.run_tx("schedule", "--steward", "central").stdout)
        self.assertTrue(
            any(
                update.get("request_id") == optimistic["request_id"]
                and update.get("action") == "activated"
                for update in completed
            )
        )

    def test_active_claim_cannot_expand_into_an_older_exclusive_queue(self) -> None:
        self.custom_claim("notes-a", "agent-a", "notes.txt", "note:a")
        self.custom_claim(
            "notes-refactor",
            "agent-r",
            "notes.txt",
            "contract:notes",
            intent="refactor",
        )
        request = self.enqueue(("notes-refactor",), "exclusive")
        self.custom_claim("health", "agent-h", "src/router.txt", "route:/health")

        rejected = self.run_coord(
            "update",
            "--scope",
            "health",
            "--owner",
            "agent-h",
            "--add-paths",
            "notes.txt",
            "--allow-overlap",
            "--reason",
            "Attempted late scope expansion",
            expected=1,
        )

        self.assertIn("claim expansion would bypass", rejected.stderr)
        self.assertIn(str(request["request_id"]), rejected.stderr)

    def test_publish_dependency_cycle_is_rejected_before_materialization(self) -> None:
        self.custom_claim(
            "health",
            "agent-a",
            "src/router.txt",
            "route:/health",
            depends_on=("metrics",),
        )
        self.custom_claim(
            "metrics",
            "agent-b",
            "src/router.txt",
            "route:/metrics",
            depends_on=("health",),
        )

        rejected = self.run_tx(
            "enqueue",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            "parallel-tx",
            "--steward",
            "central",
            "--reason",
            "Invalid cyclic dependency",
            expected=1,
        )

        self.assertIn("scope dependency cycle", rejected.stderr)
        direct = self.run_tx(
            "begin",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            "parallel-tx",
            "--steward",
            "central",
            "--reason",
            "The same invalid dependency without the queue",
            expected=1,
        )
        self.assertIn("scope dependency cycle", direct.stderr)
        self.assertEqual(
            list((self.repo / ".agent-coordination" / "waiting" / "active").glob("*.json")),
            [],
        )
        self.assertEqual(
            self.run_git("branch", "--list", "agent-tx/*").stdout.strip(), ""
        )

    def test_changed_claim_snapshot_stops_scheduling_for_attention(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        request = self.enqueue(("health", "metrics"), "parallel-tx")
        self.run_coord(
            "update",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--semantic-writes",
            "route:/health-v2",
            "--allow-overlap",
            "--reason",
            "The semantic unit changed after arbitration",
        )

        updates = json.loads(self.run_tx("schedule", "--steward", "central").stdout)

        attention = next(
            update
            for update in updates
            if update.get("request_id") == request["request_id"]
        )
        self.assertEqual(attention["action"], "needs-attention")
        self.assertTrue(
            any(
                "changed field 'semantic_writes'" in blocker
                for blocker in attention["blockers"]
            )
        )
        self.assertEqual(
            self.run_git("branch", "--list", "agent-tx/*").stdout.strip(), ""
        )

    def test_reconcile_recovers_request_after_group_plan_crash(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        request = self.enqueue(("health", "metrics"), "parallel-tx")
        self.run_tx(
            "schedule",
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "group-planned"},
        )
        self.assertEqual(self.active_requests()[0]["status"], "activating")

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)

        self.assertTrue(
            any(
                update.get("request_id") == request["request_id"]
                and update.get("action") == "activated"
                for update in updates
            )
        )
        self.assertEqual(self.active_requests(), [])
        status = json.loads(self.run_tx("status", "--json").stdout)
        self.assertEqual(status["groups"][0]["status"], "active")

    def test_reconcile_recovers_exclusive_grant_before_request_archive(self) -> None:
        self.custom_claim("health", "agent-a", "src/router.txt", "route:/health")
        self.custom_claim(
            "refactor",
            "agent-r",
            "src/router.txt",
            "contract:routing",
            intent="refactor",
        )
        request = self.enqueue(("refactor",), "exclusive")
        self.run_coord(
            "release",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--summary",
            "Release blocker",
        )
        self.run_tx(
            "schedule",
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "exclusive-claim-granted"},
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)

        self.assertTrue(
            any(
                update.get("request_id") == request["request_id"]
                and update.get("action") == "activated"
                for update in updates
            )
        )
        self.assertEqual(self.active_requests(), [])

    def test_reconcile_recovers_wait_grant_before_request_archive(self) -> None:
        self.custom_claim("health", "agent-a", "src/router.txt", "route:/health")
        self.custom_claim("metrics", "agent-b", "src/router.txt", "route:/metrics")
        request = self.enqueue(("metrics",), "wait")
        self.run_coord(
            "release",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--summary",
            "Release the direct blocker without auto-granting a manual request",
        )
        self.run_tx(
            "schedule",
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "wait-claim-granted"},
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)

        self.assertTrue(
            any(
                update.get("request_id") == request["request_id"]
                and update.get("action") == "activated"
                for update in updates
            )
        )
        metrics_path = self.repo / ".agent-coordination" / "claims" / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        self.assertEqual(metrics["status"], "active")
        self.assertEqual(metrics["wait_request_id"], request["request_id"])
        self.assertEqual(self.active_requests(), [])

    def test_request_cancellation_requires_exact_owners(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        request = self.enqueue(("health", "metrics"), "parallel-tx")

        rejected = self.run_tx(
            "cancel-request",
            "--request",
            str(request["request_id"]),
            "--steward",
            "central",
            "--owners",
            "agent-a",
            "--reason",
            "Incomplete authorization",
            expected=1,
        )
        self.assertIn("exact owner acknowledgements", rejected.stderr)

        completed = json.loads(
            self.run_tx(
                "cancel-request",
                "--request",
                str(request["request_id"]),
                "--steward",
                "central",
                "--owners",
                "agent-a",
                "agent-b",
                "--reason",
                "Both owners cancelled the queued arbitration",
            ).stdout
        )
        self.assertEqual(completed["cancelled"]["action"], "cancelled")
        self.assertEqual(self.active_requests(), [])

    def test_hotspots_report_queue_metrics_by_path_and_semantic_resource(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        self.enqueue(("health", "metrics"), "parallel-tx")
        self.run_tx("schedule", "--steward", "central")

        report = json.loads(self.run_tx("hotspots").stdout)

        self.assertEqual(report["queue"]["requested"], 1)
        self.assertEqual(report["queue"]["activated"], 1)
        router = next(
            item for item in report["path_hotspots"] if item["resource"] == "src/router.txt"
        )
        self.assertEqual(router["queue_requests"], 1)
        self.assertEqual(router["activations"], 1)
        semantic_resources = {
            item["resource"] for item in report["semantic_hotspots"]
        }
        self.assertIn("route:/health", semantic_resources)
        self.assertIn("route:/metrics", semantic_resources)


if __name__ == "__main__":
    unittest.main()
