from __future__ import annotations

import json
import unittest

from tests.integration_support import TransactionRepositoryCase


class DistributedContentionIntegrationTest(TransactionRepositoryCase):
    def open_pair(self) -> dict[str, object]:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        records = json.loads(self.run_tx("contention-status").stdout)
        self.assertEqual(len(records), 1)
        return records[0]

    def propose(
        self,
        contention: dict[str, object],
        *,
        owner: str = "agent-b",
        epoch: int = 1,
        decision: str | None = None,
        targets: tuple[str, ...] = (),
        expected: int = 0,
    ):
        arguments = [
            "contention-propose",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            owner,
            "--epoch",
            str(epoch),
            "--reason",
            "Choose the cheapest safe contention-local workflow",
        ]
        if decision is not None:
            arguments.extend(("--decision", decision))
        if targets:
            arguments.extend(("--target-scopes", *targets))
        return self.run_tx(*arguments, expected=expected)

    def respond(
        self,
        contention: dict[str, object],
        owner: str,
        *,
        accept: bool = True,
        revision: int = 1,
    ) -> dict[str, object]:
        completed = self.run_tx(
            "contention-respond",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            owner,
            "--revision",
            str(revision),
            "--accept" if accept else "--reject",
            "--reason",
            "Participant reviewed the proposed semantic boundary",
        )
        return json.loads(completed.stdout)

    def test_conflict_detector_becomes_contention_local_coordinator(self) -> None:
        contention = self.open_pair()

        self.assertEqual(contention["trigger_scope"], "metrics")
        self.assertEqual(contention["coordinator"]["owner"], "agent-b")
        self.assertEqual(contention["coordinator"]["epoch"], 1)
        self.assertEqual(contention["recommendation"]["recommendation"], "parallel-tx")
        self.assertEqual(contention["status"], "open")
        self.assertEqual(contention["scopes"], ["health", "metrics"])

    def test_unanimous_parallel_decision_activates_and_closes_contention(self) -> None:
        contention = self.open_pair()
        proposed = json.loads(self.propose(contention).stdout)
        self.assertEqual(proposed["decision"]["mode"], "parallel-tx")
        self.assertEqual(proposed["status"], "awaiting-acks")
        accepted = self.respond(contention, "agent-a")
        self.assertEqual(accepted["status"], "ready")

        enacted = json.loads(
            self.run_tx(
                "contention-enact",
                "--contention",
                str(contention["contention_id"]),
                "--owner",
                "agent-b",
                "--epoch",
                "1",
            ).stdout
        )

        self.assertEqual(enacted["contentions"][0]["action"], "completed")
        self.assertEqual(json.loads(self.run_tx("contention-status").stdout), [])
        status = json.loads(self.run_tx("status", "--json").stdout)
        self.assertEqual(len(status["transactions"]), 2)
        self.assertTrue(all(item["status"] == "active" for item in status["transactions"]))

    def test_expired_lease_can_be_acquired_without_transferring_claims(self) -> None:
        contention = self.open_pair()
        record_path = next(
            (self.repo / ".agent-coordination" / "contentions" / "active").glob(
                "*.json"
            )
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["coordinator"]["lease_until"] = "2000-01-01T00:00:00Z"
        record_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        acquired = json.loads(
            self.run_tx(
                "contention-acquire",
                "--contention",
                str(contention["contention_id"]),
                "--owner",
                "agent-a",
                "--expected-epoch",
                "1",
            ).stdout
        )

        self.assertEqual(acquired["coordinator"]["owner"], "agent-a")
        self.assertEqual(acquired["coordinator"]["epoch"], 2)
        rejected = self.propose(contention, expected=1)
        self.assertIn("stale contention coordinator token", rejected.stderr)
        claims = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in (self.repo / ".agent-coordination" / "claims").glob("*.json")
        }
        self.assertEqual(claims["health"]["owner"], "agent-a")
        self.assertEqual(claims["metrics"]["owner"], "agent-b")

    def test_explicit_coordination_handoff_fences_the_prior_epoch(self) -> None:
        contention = self.open_pair()
        handed_off = json.loads(
            self.run_tx(
                "contention-handoff",
                "--contention",
                str(contention["contention_id"]),
                "--owner",
                "agent-b",
                "--epoch",
                "1",
                "--next-owner",
                "agent-a",
                "--reason",
                "Agent A is closer to the affected contract",
            ).stdout
        )
        self.assertEqual(handed_off["coordinator"]["owner"], "agent-a")
        self.assertEqual(handed_off["coordinator"]["epoch"], 2)
        stale = self.propose(contention, expected=1)
        self.assertIn("stale contention coordinator token", stale.stderr)
        current = self.propose(contention, owner="agent-a", epoch=2)
        self.assertEqual(json.loads(current.stdout)["decision"]["revision"], 1)

    def test_rejection_requires_a_new_decision_revision(self) -> None:
        contention = self.open_pair()
        self.propose(contention)
        rejected = self.respond(contention, "agent-a", accept=False)
        self.assertEqual(rejected["status"], "needs-decision")
        blocked = self.run_tx(
            "contention-enact",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            "agent-b",
            "--epoch",
            "1",
            expected=1,
        )
        self.assertIn("unanimous acceptance", blocked.stderr)
        revised = json.loads(
            self.propose(
                contention,
                decision="ordered-tx",
            ).stdout
        )
        self.assertEqual(revised["decision"]["revision"], 2)
        self.assertEqual(sorted(revised["responses"]), ["agent-b"])

    def test_claim_change_invalidates_an_accepted_decision_before_checkout(self) -> None:
        contention = self.open_pair()
        self.propose(contention)
        self.respond(contention, "agent-a")
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

        stale = self.run_tx(
            "contention-enact",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            "agent-b",
            "--epoch",
            "1",
            expected=1,
        )

        self.assertIn("decision became stale", stale.stderr)
        current = json.loads(self.run_tx("contention-status").stdout)[0]
        self.assertEqual(current["status"], "needs-decision")
        self.assertEqual(
            self.run_git("branch", "--list", "agent-tx/*").stdout.strip(), ""
        )

    def test_wait_decision_is_granted_when_the_blocking_claim_releases(self) -> None:
        contention = self.open_pair()
        self.propose(contention, decision="wait", targets=("metrics",))
        self.respond(contention, "agent-a")
        enacted = json.loads(
            self.run_tx(
                "contention-enact",
                "--contention",
                str(contention["contention_id"]),
                "--owner",
                "agent-b",
                "--epoch",
                "1",
            ).stdout
        )
        self.assertTrue(
            any(update.get("action") == "blocked" for update in enacted["queue"])
        )
        self.run_tx("contention-reconcile")
        active_contentions = json.loads(self.run_tx("contention-status").stdout)
        self.assertEqual(len(active_contentions), 1)
        self.assertEqual(
            active_contentions[0]["contention_id"], contention["contention_id"]
        )

        self.run_coord(
            "release",
            "--scope",
            "health",
            "--owner",
            "agent-a",
            "--summary",
            "The blocking direct slice completed",
        )

        metrics_path = (
            self.repo / ".agent-coordination" / "claims" / "metrics.json"
        )
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        self.assertEqual(metrics["status"], "active")
        self.assertIn("wait_request_id", metrics)
        self.assertEqual(json.loads(self.run_tx("contention-status").stdout), [])

    def test_new_participant_invalidates_an_unenacted_decision(self) -> None:
        contention = self.open_pair()
        self.propose(contention)
        self.claim("tracing", "agent-c", "route:/tracing")

        current = json.loads(self.run_tx("contention-status").stdout)[0]
        self.assertEqual(current["contention_id"], contention["contention_id"])
        self.assertEqual(current["status"], "open")
        self.assertEqual(current["scopes"], ["health", "metrics", "tracing"])
        self.assertNotIn("decision", current)

    def test_reconcile_links_a_request_created_before_contention_update(self) -> None:
        contention = self.open_pair()
        self.propose(contention)
        self.respond(contention, "agent-a")
        self.run_tx(
            "contention-enact",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            "agent-b",
            "--epoch",
            "1",
            expected=86,
            environment={
                "SHARED_COORD_TEST_CRASH_POINT": "contention-request-created"
            },
        )

        active = json.loads(self.run_tx("contention-status").stdout)[0]
        self.assertNotIn("request_id", active)
        request = json.loads(self.run_tx("status", "--json").stdout)["requests"][0]
        self.assertEqual(request["contention_id"], contention["contention_id"])

        recovered = json.loads(self.run_tx("contention-reconcile").stdout)

        self.assertTrue(
            any(update.get("action") == "request-linked" for update in recovered["updates"])
        )
        self.assertEqual(json.loads(self.run_tx("contention-status").stdout), [])
        self.assertEqual(
            len(json.loads(self.run_tx("status", "--json").stdout)["transactions"]),
            2,
        )

    def test_audit_log_and_workflow_report_preserve_the_decision_chain(self) -> None:
        contention = self.open_pair()
        self.propose(contention)
        self.respond(contention, "agent-a")
        self.run_tx(
            "contention-enact",
            "--contention",
            str(contention["contention_id"]),
            "--owner",
            "agent-b",
            "--epoch",
            "1",
        )

        audit = json.loads(
            self.run_tx(
                "log",
                "--contention",
                str(contention["contention_id"]),
            ).stdout
        )
        events = [item["event"] for item in audit["events"]]
        self.assertIn("contention-opened", events)
        self.assertIn("contention-decision-proposed", events)
        self.assertIn("contention-decision-accepted", events)
        self.assertIn("contention-enacted", events)
        self.assertIn("contention-completed", events)

        report = json.loads(self.run_tx("workflow-report").stdout)
        self.assertEqual(report["summary"]["contentions"], 1)
        self.assertEqual(report["summary"]["completed"], 1)
        self.assertEqual(report["decisions"]["parallel-tx"], 1)
        self.assertIsInstance(
            report["workflows"][0]["total_coordination_ms"], int
        )
        hotspots = json.loads(self.run_tx("hotspots").stdout)
        self.assertEqual(hotspots["coordination"]["completed"], 1)


if __name__ == "__main__":
    unittest.main()
