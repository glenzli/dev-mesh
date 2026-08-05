from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.integration_support import TransactionRepositoryCase


class PartialGroupAbortIntegrationTest(TransactionRepositoryCase):
    def abort_group(
        self,
        group_id: str,
        *,
        expected: int = 0,
        crash_point: str | None = None,
        owners: tuple[str, ...] = ("agent-a", "agent-b"),
    ):
        environment = (
            {"SHARED_COORD_TEST_CRASH_POINT": crash_point}
            if crash_point is not None
            else None
        )
        return self.run_tx(
            "abort-group",
            "--group",
            group_id,
            "--steward",
            "central",
            "--owners",
            *owners,
            "--reason",
            "All owners approved rollback of the incomplete group",
            "--discard",
            expected=expected,
            environment=environment,
        )

    def active_group_paths(self) -> list[Path]:
        return list(
            (self.repo / ".agent-coordination" / "groups" / "active").glob(
                "*.json"
            )
        )

    def active_transaction_paths(self) -> list[Path]:
        return list(
            (
                self.repo
                / ".agent-coordination"
                / "transactions"
                / "active"
            ).glob("*.json")
        )

    def assert_claims_restored(self) -> None:
        claims = self.repo / ".agent-coordination" / "claims"
        health = json.loads((claims / "health.json").read_text(encoding="utf-8"))
        metrics = json.loads((claims / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "active")
        self.assertEqual(metrics["status"], "pending-arbitration")
        self.assertNotIn("transaction_id", health)
        self.assertNotIn("transaction_id", metrics)

    def test_abort_group_rolls_back_a_planned_group_and_restores_claims(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        _, group = self.crash_begin("group-planned")

        result = json.loads(self.abort_group(str(group["group_id"])).stdout)

        self.assertEqual(result["action"], "completed")
        self.assertEqual(self.active_group_paths(), [])
        self.assertEqual(self.active_transaction_paths(), [])
        self.assert_claims_restored()
        self.assertEqual(
            self.run_git("branch", "--list", "agent-tx/*").stdout.strip(), ""
        )
        self.assertEqual(
            self.run_git("worktree", "list", "--porcelain").stdout.count("worktree "),
            1,
        )
        doctor = json.loads(self.run_tx("doctor").stdout)
        self.assertEqual(doctor["status"], "ok")

    def test_abort_group_removes_a_partially_materialized_member(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        _, group = self.crash_begin("member-materialized:health")
        health = group["members"][0]["planned_transaction"]
        checkout = Path(str(health["checkout"]))
        branch = str(health["branch"])
        self.assertTrue(checkout.exists())

        self.abort_group(str(group["group_id"]))

        self.assertFalse(checkout.exists())
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}", expected=128)
        self.assert_claims_restored()

    def test_abort_group_restores_a_claim_archived_before_barrier(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        _, group = self.crash_begin("claim-archived:health")
        claims = self.repo / ".agent-coordination" / "claims"
        self.assertFalse((claims / "health.json").exists())

        self.abort_group(str(group["group_id"]))

        self.assert_claims_restored()
        archived_groups = list(
            (self.repo / ".agent-coordination" / "groups" / "archive").glob(
                "*.json"
            )
        )
        self.assertEqual(len(archived_groups), 1)
        archived = json.loads(archived_groups[0].read_text(encoding="utf-8"))
        self.assertEqual(archived["status"], "aborted")

    def test_abort_group_requires_exact_owner_acknowledgements(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("group-planned")

        rejected = self.abort_group(
            str(group["group_id"]), expected=1, owners=("agent-a",)
        )

        self.assertIn("exact owner acknowledgements", rejected.stderr)
        self.assertEqual(
            json.loads(group_path.read_text(encoding="utf-8"))["status"], "planned"
        )

    def test_abort_group_rejects_a_fully_active_group(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()

        rejected = self.abort_group(str(health["group_id"]), expected=1)

        self.assertIn("require each transaction owner to use normal abort", rejected.stderr)
        self.assertEqual(len(self.active_group_paths()), 1)
        self.assertEqual(len(self.active_transaction_paths()), 2)

    def test_reconcile_never_invents_a_missing_cleanup_authorization(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("group-planned")
        self.abort_group(
            str(group["group_id"]),
            expected=86,
            crash_point="group-abort-cleanup-authorized:health",
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)

        abort_update = next(item for item in updates if item["kind"] == "group-abort")
        self.assertEqual(abort_update["action"], "needs-attention")
        self.assertIn("no owner-authorized cleanup snapshot", " ".join(abort_update["issues"]))
        self.assertEqual(
            json.loads(group_path.read_text(encoding="utf-8"))["status"],
            "abort-needs-attention",
        )
        self.assert_claims_restored()

        completed = json.loads(self.abort_group(str(group["group_id"])).stdout)
        self.assertEqual(completed["action"], "completed")
        self.assertEqual(self.active_group_paths(), [])

    def test_content_changed_after_group_authorization_requires_fresh_approval(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        _, group = self.crash_begin("member-materialized:health")
        health = group["members"][0]["planned_transaction"]
        checkout = Path(str(health["checkout"]))
        router = checkout / "src" / "router.txt"
        self.abort_group(
            str(group["group_id"]),
            expected=86,
            crash_point="group-abort-authorized",
        )
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=late"),
            encoding="utf-8",
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)

        abort_update = next(item for item in updates if item["kind"] == "group-abort")
        self.assertEqual(abort_update["action"], "needs-attention")
        self.assertTrue(checkout.exists())
        self.assertIn("health=late", router.read_text(encoding="utf-8"))

        self.run_tx(
            "cleanup-authorize",
            "--transaction",
            str(health["transaction_id"]),
            "--owner",
            "agent-a",
            "--reason",
            "Agent A reviewed and approved the late content discard",
            "--discard",
        )
        completed = json.loads(
            self.run_tx("reconcile", "--steward", "central").stdout
        )
        self.assertTrue(
            any(
                item.get("kind") == "group-abort" and item.get("action") == "completed"
                for item in completed
            )
        )
        self.assertFalse(checkout.exists())
        self.assert_claims_restored()


if __name__ == "__main__":
    unittest.main()
