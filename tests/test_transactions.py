from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.integration_support import TransactionRepositoryCase


class TransactionIntegrationTest(TransactionRepositoryCase):
    def test_semantic_inspection_recommends_parallel_transaction(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        result = json.loads(
            self.run_tx("inspect", "--scopes", "health", "metrics").stdout
        )
        self.assertEqual(result["recommendation"], "parallel-tx")
        self.assertTrue(result["relationships"]["physical"])

    def test_parallel_transactions_refresh_and_preserve_unrelated_dirty_work(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, metrics = self.begin_pair()

        health_file = Path(str(health["checkout"])) / "src" / "router.txt"
        metrics_file = Path(str(metrics["checkout"])) / "src" / "router.txt"
        health_file.write_text(
            health_file.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        metrics_file.write_text(
            metrics_file.read_text(encoding="utf-8").replace("metrics=off", "metrics=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        self.prepare_validate(metrics, "Enable metrics route")

        (self.repo / "notes.txt").write_text("unrelated dirty work\n", encoding="utf-8")
        self.run_tx(
            "publish",
            "--transaction",
            str(health["transaction_id"]),
            "--steward",
            "central",
        )

        refreshed = self.run_tx(
            "publish",
            "--transaction",
            str(metrics["transaction_id"]),
            "--steward",
            "central",
            expected=2,
        )
        self.assertEqual(json.loads(refreshed.stdout)["status"], "prepared")
        self.run_tx(
            "validate",
            "--transaction",
            str(metrics["transaction_id"]),
            "--owner",
            str(metrics["owner"]),
            "--evidence",
            "focused test passed after refresh",
        )
        self.run_tx(
            "publish",
            "--transaction",
            str(metrics["transaction_id"]),
            "--steward",
            "central",
        )

        self.assertEqual(
            list((self.repo / ".agent-coordination" / "groups" / "active").glob("*.json")),
            [],
        )
        group_archives = list(
            (self.repo / ".agent-coordination" / "groups" / "archive").glob("*.json")
        )
        self.assertEqual(len(group_archives), 1)
        self.assertEqual(
            json.loads(group_archives[0].read_text(encoding="utf-8"))["status"],
            "closed",
        )

        router = (self.repo / "src" / "router.txt").read_text(encoding="utf-8")
        self.assertIn("health=on", router)
        self.assertIn("metrics=on", router)
        self.assertEqual(
            (self.repo / "notes.txt").read_text(encoding="utf-8"),
            "unrelated dirty work\n",
        )
        self.assertEqual(self.run_git("diff", "--cached", "--name-only").stdout, "")

    def test_dirty_overlap_blocks_promotion(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        router = self.repo / "src" / "router.txt"
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=dirty"),
            encoding="utf-8",
        )
        completed = self.run_tx(
            "begin",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            "parallel-tx",
            "--steward",
            "central",
            "--reason",
            "Independent entries",
            expected=1,
        )
        self.assertIn("overlapping writes started", completed.stderr)

    def test_nonempty_shared_index_blocks_publish(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        health_file = Path(str(health["checkout"])) / "src" / "router.txt"
        health_file.write_text(
            health_file.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        (self.repo / "notes.txt").write_text("staged work\n", encoding="utf-8")
        self.run_git("add", "notes.txt")
        completed = self.run_tx(
            "publish",
            "--transaction",
            str(health["transaction_id"]),
            "--steward",
            "central",
            expected=1,
        )
        self.assertIn("index must be empty", completed.stderr)

    def test_publish_requires_the_canonical_branch(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        health_file = Path(str(health["checkout"])) / "src" / "router.txt"
        health_file.write_text(
            health_file.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        self.run_git("switch", "-c", "side")
        completed = self.run_tx(
            "publish",
            "--transaction",
            str(health["transaction_id"]),
            "--steward",
            "central",
            expected=1,
        )
        self.assertIn("canonical workspace is on branch 'side', expected 'main'", completed.stderr)

    def test_refresh_conflict_is_isolated_from_canonical_workspace(self) -> None:
        self.claim("health", "agent-a", "route:/health-a")
        self.claim("metrics", "agent-b", "route:/health-b")
        first, second = self.begin_pair()
        for record, replacement in ((first, "health=one"), (second, "health=two")):
            path = Path(str(record["checkout"])) / "src" / "router.txt"
            path.write_text(
                path.read_text(encoding="utf-8").replace("health=off", replacement),
                encoding="utf-8",
            )
            self.prepare_validate(record, replacement)
        self.run_tx(
            "publish",
            "--transaction",
            str(first["transaction_id"]),
            "--steward",
            "central",
        )
        conflicted = self.run_tx(
            "publish",
            "--transaction",
            str(second["transaction_id"]),
            "--steward",
            "central",
            expected=2,
        )
        self.assertEqual(json.loads(conflicted.stdout)["status"], "conflicted")
        self.assertIn("health=one", (self.repo / "src" / "router.txt").read_text())
        self.assertFalse((self.repo / ".git" / "rebase-merge").exists())
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_overlapping_request_is_pending_and_does_not_block_publish(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        self.claim("tracing", "agent-c", "route:/tracing")
        pending = json.loads(
            (
                self.repo
                / ".agent-coordination"
                / "claims"
                / "tracing.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(pending["status"], "pending-arbitration")

        health_file = Path(str(health["checkout"])) / "src" / "router.txt"
        health_file.write_text(
            health_file.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        self.run_tx(
            "publish",
            "--transaction",
            str(health["transaction_id"]),
            "--steward",
            "central",
        )

    def test_ordered_transaction_cannot_publish_before_dependency(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        _, metrics = self.begin_pair(mode="ordered-tx")
        metrics_file = Path(str(metrics["checkout"])) / "src" / "router.txt"
        metrics_file.write_text(
            metrics_file.read_text(encoding="utf-8").replace("metrics=off", "metrics=on"),
            encoding="utf-8",
        )
        self.prepare_validate(metrics, "Enable metrics route")
        completed = self.run_tx(
            "publish",
            "--transaction",
            str(metrics["transaction_id"]),
            "--steward",
            "central",
            expected=1,
        )
        self.assertIn("publish dependency is not committed", completed.stderr)

    def test_prepare_rejects_out_of_scope_change(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        checkout = Path(str(health["checkout"]))
        (checkout / "notes.txt").write_text("unauthorized change\n", encoding="utf-8")
        completed = self.run_tx(
            "prepare",
            "--transaction",
            str(health["transaction_id"]),
            "--owner",
            str(health["owner"]),
            "--summary",
            "Unauthorized change",
            expected=1,
        )
        self.assertIn("exceeds its claim", completed.stderr)

    def test_handoff_pauses_until_new_owner_resumes(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        handed_off = json.loads(
            self.run_tx(
                "handoff",
                "--transaction",
                str(health["transaction_id"]),
                "--owner",
                "agent-a",
                "--next-owner",
                "agent-c",
                "--checkpoint",
                "No edits yet; continue from the materialized base",
            ).stdout
        )
        self.assertEqual(handed_off["status"], "paused")
        resumed = json.loads(
            self.run_tx(
                "resume",
                "--transaction",
                str(health["transaction_id"]),
                "--owner",
                "agent-c",
            ).stdout
        )
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["owner"], "agent-c")
        events = json.loads(
            self.run_tx(
                "log",
                "--transaction",
                str(health["transaction_id"]),
            ).stdout
        )["events"]
        recorded = next(
            event for event in events if event["event"] == "transaction-recorded"
        )
        self.assertEqual(recorded["trace_schema"], 1)
        self.assertEqual(recorded["owner"], "agent-a")
        self.assertEqual(recorded["scope"], "health")
        self.assertEqual(recorded["branch"], health["branch"])
        self.assertEqual(recorded["base_revision"], health["base_revision"])
        self.assertEqual(recorded["canonical_branch"], "main")
        self.assertEqual(recorded["mode"], "parallel-tx")
        handed_off_event = next(
            event for event in events if event["event"] == "transaction-handed-off"
        )
        self.assertEqual(handed_off_event["actor_owner"], "agent-a")
        self.assertEqual(handed_off_event["work_owner"], "agent-c")
        self.assertEqual(handed_off_event["source_owner"], "agent-a")
        self.assertEqual(handed_off_event["target_owner"], "agent-c")
        resumed_event = next(
            event for event in events if event["event"] == "transaction-resumed"
        )
        self.assertEqual(resumed_event["owner"], "agent-c")


if __name__ == "__main__":
    unittest.main()
