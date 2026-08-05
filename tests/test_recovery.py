from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.integration_support import TransactionRepositoryCase


class TransactionRecoveryIntegrationTest(TransactionRepositoryCase):
    def test_begin_activates_one_durable_group_before_granting_authority(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, metrics = self.begin_pair()
        self.assertEqual(health["group_id"], metrics["group_id"])
        group_path = (
            self.repo
            / ".agent-coordination"
            / "groups"
            / "active"
            / f"{health['group_id']}.json"
        )
        group = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertEqual(group["status"], "active")
        self.assertTrue(group["claims_promoted"])
        self.assertEqual(
            list((self.repo / ".agent-coordination" / "claims").glob("*.json")),
            [],
        )

    def test_reconcile_recovers_a_crash_after_group_planning_idempotently(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("group-planned")
        self.assertEqual(group["status"], "planned")
        self.assertEqual(
            list(
                (
                    self.repo
                    / ".agent-coordination"
                    / "transactions"
                    / "active"
                ).glob("*.json")
            ),
            [],
        )
        status = json.loads(self.run_tx("status", "--json").stdout)
        self.assertEqual(status["groups"][0]["status"], "planned")
        self.assertEqual(status["transactions"], [])

        recovered = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertEqual(recovered[0]["action"], "activated")
        group = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertEqual(group["status"], "active")
        self.assertTrue(group["claims_promoted"])
        repeated = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertEqual(repeated[0]["action"], "unchanged")
        self.assertEqual(
            self.run_git("worktree", "list", "--porcelain").stdout.count("worktree "),
            3,
        )

    def test_reconcile_rematerializes_an_existing_transaction_branch(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("member-materialized:health")
        health = group["members"][0]["planned_transaction"]
        checkout = Path(str(health["checkout"]))
        branch = str(health["branch"])
        self.run_git("worktree", "remove", str(checkout))
        self.assertFalse(checkout.exists())
        self.run_git("show-ref", "--verify", f"refs/heads/{branch}")

        self.run_tx("reconcile", "--steward", "central")
        self.assertTrue(checkout.exists())
        group = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertEqual(group["status"], "active")
        self.assertTrue(group["claims_promoted"])

    def test_reconcile_preserves_dirty_materializing_checkout_for_attention(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("member-materialized:health")
        health = group["members"][0]["planned_transaction"]
        checkout = Path(str(health["checkout"]))
        router = checkout / "src" / "router.txt"
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=uncertain"),
            encoding="utf-8",
        )

        result = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertEqual(result[0]["action"], "needs-attention")
        group = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertEqual(group["status"], "needs-attention")
        self.assertIn("health=uncertain", router.read_text(encoding="utf-8"))
        self.run_command("git", "-C", str(checkout), "restore", "src/router.txt")
        self.run_tx("reconcile", "--steward", "central")
        self.assertEqual(
            json.loads(group_path.read_text(encoding="utf-8"))["status"],
            "active",
        )

    def test_reconcile_preserves_an_unexpected_materializing_commit(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("member-materialized:health")
        health = group["members"][0]["planned_transaction"]
        checkout = Path(str(health["checkout"]))
        router = checkout / "src" / "router.txt"
        router.write_text(
            router.read_text(encoding="utf-8").replace("health=off", "health=committed-early"),
            encoding="utf-8",
        )
        self.run_command("git", "-C", str(checkout), "add", "src/router.txt")
        self.run_command("git", "-C", str(checkout), "commit", "-m", "unexpected early work")
        unexpected_head = self.run_command(
            "git", "-C", str(checkout), "rev-parse", "HEAD"
        ).stdout.strip()

        result = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertEqual(result[0]["action"], "needs-attention")
        self.assertIn("expected materialization base", result[0]["issue"])
        self.assertEqual(
            self.run_command("git", "-C", str(checkout), "rev-parse", "HEAD").stdout.strip(),
            unexpected_head,
        )
        self.assertEqual(
            json.loads(group_path.read_text(encoding="utf-8"))["status"],
            "needs-attention",
        )

    def test_group_activation_crash_withholds_authority_until_claim_recovery(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("group-activated")
        self.assertEqual(group["status"], "active")
        self.assertFalse(group["claims_promoted"])
        health = group["members"][0]["planned_transaction"]
        blocked = self.run_tx(
            "prepare",
            "--transaction",
            str(health["transaction_id"]),
            "--owner",
            str(health["owner"]),
            "--summary",
            "Should remain blocked",
            expected=1,
        )
        self.assertIn("is not fully active", blocked.stderr)

        self.run_tx("reconcile", "--steward", "central")
        recovered = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertTrue(recovered["claims_promoted"])
        self.assertEqual(
            list((self.repo / ".agent-coordination" / "claims").glob("*.json")),
            [],
        )

    def test_reconcile_recognizes_a_claim_archived_before_snapshot_update(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        group_path, group = self.crash_begin("claim-archived:health")
        self.assertFalse(
            (self.repo / ".agent-coordination" / "claims" / "health.json").exists()
        )
        self.assertFalse(group["claims_promoted"])

        self.run_tx("reconcile", "--steward", "central")
        recovered = json.loads(group_path.read_text(encoding="utf-8"))
        self.assertTrue(recovered["claims_promoted"])
        self.assertTrue(all(member["claim_promoted"] for member in recovered["members"]))

    def test_reconcile_completes_publish_after_fast_forward_crash(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, _ = self.begin_pair()
        health_file = Path(str(health["checkout"])) / "src" / "router.txt"
        health_file.write_text(
            health_file.read_text(encoding="utf-8").replace("health=off", "health=on"),
            encoding="utf-8",
        )
        self.prepare_validate(health, "Enable health route")
        transaction_path = (
            self.repo
            / ".agent-coordination"
            / "transactions"
            / "active"
            / f"{health['transaction_id']}.json"
        )
        candidate = str(
            json.loads(transaction_path.read_text(encoding="utf-8"))["candidate"]
        )
        self.run_tx(
            "publish",
            "--transaction",
            str(health["transaction_id"]),
            "--steward",
            "central",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "publish-fast-forwarded"},
        )
        self.assertEqual(self.run_git("rev-parse", "HEAD").stdout.strip(), candidate)

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        completed = [item for item in updates if item.get("action") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertFalse(transaction_path.exists())
        self.assertIn("health=on", (self.repo / "src" / "router.txt").read_text())

    def test_reconcile_archives_a_group_closed_before_its_move(self) -> None:
        self.claim("health", "agent-a", "route:/health")
        self.claim("metrics", "agent-b", "route:/metrics")
        health, metrics = self.begin_pair()
        for record, marker, replacement in (
            (health, "health=off", "health=on"),
            (metrics, "metrics=off", "metrics=on"),
        ):
            router = Path(str(record["checkout"])) / "src" / "router.txt"
            router.write_text(
                router.read_text(encoding="utf-8").replace(marker, replacement),
                encoding="utf-8",
            )
            self.prepare_validate(record, replacement)

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
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": "group-closed-recorded"},
        )
        group_paths = list(
            (self.repo / ".agent-coordination" / "groups" / "active").glob("*.json")
        )
        self.assertEqual(len(group_paths), 1)
        self.assertEqual(
            json.loads(group_paths[0].read_text(encoding="utf-8"))["status"],
            "closed",
        )

        updates = json.loads(self.run_tx("reconcile", "--steward", "central").stdout)
        self.assertTrue(any(item.get("action") == "closed" for item in updates))
        self.assertTrue(any(item.get("action") == "completed" for item in updates))
        self.assertEqual(
            list((self.repo / ".agent-coordination" / "groups" / "active").glob("*.json")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
