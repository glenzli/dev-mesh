from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.integration_support import TransactionRepositoryCase


class TransactionMaintenanceIntegrationTest(TransactionRepositoryCase):
    def ready_health_transaction(self) -> dict[str, object]:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        router = Path(str(health["checkout"])) / "src" / "router.txt"
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        return health

    def test_reconcile_finishes_cleanup_after_transaction_archive_crash(self) -> None:
        health = self.ready_health_transaction()
        transaction_id = str(health["transaction_id"])
        checkout = Path(str(health["checkout"]))
        branch = str(health["branch"])
        self.run_tx(
            "publish",
            "--transaction",
            transaction_id,
            "--steward",
            "central",
            expected=86,
            environment={
                "SHARED_COORD_TEST_CRASH_POINT": "transaction-archived-before-cleanup"
            },
        )
        cleanup_path = (
            self.repo
            / ".agent-coordination"
            / "cleanups"
            / "active"
            / f"{transaction_id}.json"
        )
        self.assertTrue(cleanup_path.exists())
        self.assertTrue(checkout.exists())
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}")
        status = json.loads(self.run_tx("status", "--json").stdout)
        self.assertEqual(status["cleanups"][0]["status"], "planned")

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertTrue(
            any(
                item.get("kind") == "cleanup" and item.get("action") == "completed"
                for item in updates
            )
        )
        self.assertFalse(cleanup_path.exists())
        self.assertFalse(checkout.exists())
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}", expected=128)

    def test_reconcile_resumes_after_worktree_removal(self) -> None:
        health = self.ready_health_transaction()
        transaction_id = str(health["transaction_id"])
        checkout = Path(str(health["checkout"]))
        branch = str(health["branch"])
        self.run_tx(
            "publish",
            "--transaction",
            transaction_id,
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "cleanup-worktree-removed"},
        )
        self.assertFalse(checkout.exists())
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}")
        report = json.loads(self.run_tx("doctor").stdout)
        self.assertEqual(report["status"], "ok")

        self.run_tx("reconcile", "--steward", "central")
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}", expected=128)
        self.assertEqual(
            list(
                (
                    self.repo
                    / ".agent-coordination"
                    / "cleanups"
                    / "active"
                ).glob("*.json")
            ),
            [],
        )

    def test_reconcile_archives_cleanup_completed_before_its_move(self) -> None:
        health = self.ready_health_transaction()
        transaction_id = str(health["transaction_id"])
        self.run_tx(
            "publish",
            "--transaction",
            transaction_id,
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "cleanup-completed-recorded"},
        )
        cleanup_path = (
            self.repo
            / ".agent-coordination"
            / "cleanups"
            / "active"
            / f"{transaction_id}.json"
        )
        self.assertEqual(
            json.loads(cleanup_path.read_text(encoding="utf-8"))["status"],
            "completed",
        )

        self.run_tx("reconcile", "--steward", "central")
        self.assertFalse(cleanup_path.exists())
        self.assertEqual(
            len(
                list(
                    (
                        self.repo
                        / ".agent-coordination"
                        / "cleanups"
                        / "archive"
                    ).glob(f"*-{transaction_id}.json")
                )
            ),
            1,
        )

    def test_discard_cleanup_stops_if_target_changes_after_authorization(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        transaction_id = str(health["transaction_id"])
        router = Path(str(health["checkout"])) / "src" / "router.txt"
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=discard-me"),
            encoding="utf-8",
        )
        self.run_tx(
            "abort",
            "--transaction",
            transaction_id,
            "--owner",
            "agent-a",
            "--reason",
            "Superseded",
            "--discard",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "cleanup-planned"},
        )
        router.write_text(
            router.read_text(encoding="utf-8").replace("metrics=off", "metrics=changed-later"),
            encoding="utf-8",
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        attention = [
            item
            for item in updates
            if item.get("kind") == "cleanup" and item.get("action") == "needs-attention"
        ]
        self.assertEqual(len(attention), 1)
        self.assertIn("fresh owner approval", attention[0]["issue"])
        self.assertTrue(router.exists())
        self.assertIn("metrics=changed-later", router.read_text(encoding="utf-8"))

        completed = json.loads(
            self.run_tx(
                "cleanup-authorize",
                "--transaction",
                transaction_id,
                "--owner",
                "agent-a",
                "--reason",
                "Discard the newly reviewed checkpoint as well",
                "--discard",
            ).stdout
        )
        self.assertEqual(completed["action"], "completed")
        self.assertFalse(router.exists())

    def test_doctor_reports_orphans_without_mutating_them(self) -> None:
        orphan_checkout = (
            self.repo / ".agent-coordination" / "checkouts" / "orphan-checkout"
        )
        self.run_git("branch", "agent-tx/orphan-branch")
        self.run_git(
            "worktree",
            "add",
            "-b",
            "agent-tx/orphan-worktree",
            str(orphan_checkout),
            "HEAD",
        )

        report = json.loads(self.run_tx("doctor").stdout)
        kinds = {finding["kind"] for finding in report["findings"]}
        self.assertEqual(report["status"], "attention")
        self.assertIn("orphan-branch", kinds)
        self.assertIn("orphan-worktree", kinds)
        self.assertTrue(orphan_checkout.exists())
        self.run_git("show-ref", "--verify", "refs/heads/agent-tx/orphan-branch")
        self.run_git("show-ref", "--verify", "refs/heads/agent-tx/orphan-worktree")


if __name__ == "__main__":
    unittest.main()
