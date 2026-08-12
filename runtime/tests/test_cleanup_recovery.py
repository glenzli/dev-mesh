from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from dev_mesh_coord import (
    contention,
    transaction_cleanup,
    transaction_materialization,
    transactions,
)
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    create_claim,
    join_run,
    leave_run,
    recover_run_authority,
)

from helpers import GitWorkspaceTest, git


class CleanupRecoveryTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="isolated")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
            semantic_writes=["left"],
        )
        pending = create_claim(
            self.root,
            scope="isolated",
            owner="agent-b",
            run_id="run-b",
            task="isolated",
            paths=["app.txt"],
            semantic_writes=["right"],
            allow_overlap=True,
        )
        self.contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=self.contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="parallel-tx",
            reason="independent one-commit slice",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (("primary", "agent-a", "run-a"), ("isolated", "agent-b", "run-b")):
            contention.respond(
                self.root,
                contention_id=self.contention_id,
                scope=scope,
                owner=owner,
                run_id=run_id,
                revision=revision,
                accept=True,
            )
        contention.enact(
            self.root,
            contention_id=self.contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
        )

    def _begin(self) -> dict[str, object]:
        return transactions.begin(
            self.root,
            scope="isolated",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="cleanup recovery test",
        )

    def test_cleanup_reconcile_continues_after_worktree_effect(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        with mock.patch.object(
            transaction_cleanup,
            "reconcile_one",
            return_value={"status": "planned", "cleanup_id": transaction_id},
        ):
            transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="test-interruption",
                reason="persist cleanup intent but stop before cleanup",
                discard=True,
            )
        git(self.root, "worktree", "remove", "--force", str(checkout))
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["cleanups"][0]["status"], "completed")
        self.assertFalse((resolve(self.root).state_root / f"cleanups/active/{transaction_id}.json").exists())
        self.assertEqual(transactions.doctor(self.root)["cleanup_attention"], [])

    def test_abort_authorization_recovers_before_terminal_event_or_cleanup_intent(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        with mock.patch.object(
            transaction_cleanup,
            "plan",
            side_effect=RuntimeError("injected stop before cleanup intent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before cleanup intent"):
                transactions.abort(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    reason_code="test-interruption",
                    reason="persist abort authorization before terminal state",
                    discard=True,
                )
        plane = resolve(self.root)
        active = json.loads(
            plane.state_root.joinpath(
                f"transactions/active/{transaction_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(active["status"], "aborting")
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in plane.state_root.joinpath("events").glob("*.json")
        ]
        self.assertFalse(any(event.get("event") == "transaction-aborted" for event in events))

        reconciled = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(reconciled["aborts"][0]["action"], "abort-completed")
        self.assertEqual(reconciled["aborts"][0]["result"]["cleanup"]["status"], "completed")

    def test_reconcile_completes_initializing_and_active_aborted_states(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        plane = resolve(self.root)
        transaction_path = plane.state_root / f"transactions/active/{transaction_id}.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["status"] = "initializing"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        for event_path in plane.state_root.joinpath("events").glob("*.json"):
            event = json.loads(event_path.read_text(encoding="utf-8"))
            if event.get("event") == "transaction-created" and event.get("transaction_id") == transaction_id:
                event_path.unlink()
        initialized = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(initialized["aborts"], [])
        repaired = json.loads(transaction_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["status"], "active")

        repaired.update(
            {
                "status": "aborted",
                "reason_code": "injected-terminal-gap",
                "reason": "Terminal snapshot advanced before Claim restore.",
            }
        )
        transaction_path.write_text(json.dumps(repaired), encoding="utf-8")
        finished = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(finished["aborts"][0]["action"], "abort-completed")
        self.assertFalse(transaction_path.exists())

    def test_handoff_retries_after_claim_advanced_before_transaction(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        join_run(self.root, run_id="run-c", owner="agent-c", task="receive transaction")
        plane = resolve(self.root)
        claim_path = plane.state_root / "claims/isolated.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"owner": "agent-c", "run_id": "run-c"})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        handed = transactions.handoff(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            next_owner="agent-c",
            next_run_id="run-c",
            checkpoint="Claim projection advanced first",
        )
        self.assertEqual(handed["owner"], "agent-c")
        self.assertEqual(handed["run_id"], "run-c")

    def test_initializing_transaction_without_claim_promotion_rolls_back(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        plane = resolve(self.root)
        transaction_path = plane.state_root / f"transactions/active/{transaction_id}.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["status"] = "initializing"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        claim_path = plane.state_root / "claims/isolated.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"status": "pending-arbitration", "transaction_id": None})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rolled-back")
        self.assertFalse(Path(str(started["checkout"])).exists())

    def test_transaction_intent_precedes_git_materialization(self) -> None:
        observed_intent = False
        original = transactions._run_git

        def inspect_before_git(root: Path, *arguments: str, **kwargs: object):
            nonlocal observed_intent
            if arguments[:2] == ("worktree", "add"):
                active = list(
                    resolve(self.root).state_root.joinpath("transactions/active").glob("*.json")
                )
                self.assertEqual(len(active), 1)
                record = json.loads(active[0].read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "initializing")
                observed_intent = True
            return original(root, *arguments, **kwargs)

        with mock.patch.object(transactions, "_run_git", side_effect=inspect_before_git):
            started = self._begin()
        self.assertTrue(observed_intent)
        self.assertEqual(started["status"], "active")

    def test_failed_begin_keeps_intent_until_failed_git_cleanup_is_reconciled(self) -> None:
        original_replace = transactions.replace_json
        original_git = transaction_materialization._run_git

        def fail_claim_promotion(path: Path, value: dict[str, object], **kwargs: object):
            if path.name == "isolated.json" and value.get("status") == "transaction":
                raise RuntimeError("injected Claim promotion failure")
            return original_replace(path, value, **kwargs)

        def fail_rollback(root: Path, *arguments: str, **kwargs: object):
            if arguments[:2] == ("worktree", "remove") or arguments[:2] == ("branch", "-D"):
                return mock.Mock(returncode=1, stdout="", stderr="injected cleanup failure")
            return original_git(root, *arguments, **kwargs)

        with mock.patch.object(transactions, "replace_json", side_effect=fail_claim_promotion):
            with mock.patch.object(
                transaction_materialization,
                "_run_git",
                side_effect=fail_rollback,
            ):
                with self.assertRaisesRegex(RuntimeError, "Claim promotion"):
                    self._begin()
        plane = resolve(self.root)
        active_paths = list(plane.state_root.joinpath("transactions/active").glob("*.json"))
        self.assertEqual(len(active_paths), 1)
        retained = json.loads(active_paths[0].read_text(encoding="utf-8"))
        self.assertTrue(retained["initialization_rollback_pending"])
        self.assertTrue(Path(str(retained["checkout"])).exists())
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rolled-back")
        self.assertFalse(Path(str(retained["checkout"])).exists())
        self.assertEqual(transactions.doctor(self.root)["orphan_branches"], [])

    def test_initialization_rollback_finishes_after_only_worktree_was_removed(self) -> None:
        original_replace = transactions.replace_json
        original_git = transaction_materialization._run_git

        def fail_claim_promotion(path: Path, value: dict[str, object], **kwargs: object):
            if path.name == "isolated.json" and value.get("status") == "transaction":
                raise RuntimeError("injected Claim promotion failure")
            return original_replace(path, value, **kwargs)

        def remove_worktree_only(
            root: Path, *arguments: str, **kwargs: object
        ):
            if arguments[:2] == ("branch", "-D"):
                return mock.Mock(returncode=1, stdout="", stderr="injected branch removal failure")
            return original_git(root, *arguments, **kwargs)

        with mock.patch.object(transactions, "replace_json", side_effect=fail_claim_promotion):
            with mock.patch.object(
                transaction_materialization,
                "_run_git",
                side_effect=remove_worktree_only,
            ):
                with self.assertRaisesRegex(RuntimeError, "Claim promotion"):
                    self._begin()
        plane = resolve(self.root)
        active_paths = list(plane.state_root.joinpath("transactions/active").glob("*.json"))
        self.assertEqual(len(active_paths), 1)
        retained = json.loads(active_paths[0].read_text(encoding="utf-8"))
        self.assertFalse(Path(str(retained["checkout"])).exists())
        self.assertEqual(
            git(self.root, "rev-parse", str(retained["branch"])),
            retained["base_revision"],
        )
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rolled-back")
        self.assertEqual(transactions.doctor(self.root)["orphan_branches"], [])
        self.assertFalse(active_paths[0].exists())

    def test_initialization_rollback_finishes_after_only_branch_was_removed(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        git(self.root, "update-ref", "-d", f"refs/heads/{started['branch']}")
        plane = resolve(self.root)
        transaction_path = plane.state_root / f"transactions/active/{transaction_id}.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["status"] = "initializing"
        transaction["initialization_rollback_pending"] = True
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        claim_path = plane.state_root / "claims/isolated.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"status": "pending-arbitration", "transaction_id": None})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rolled-back")
        self.assertFalse(checkout.exists())
        self.assertFalse(transaction_path.exists())

    def test_changed_initialization_checkout_fails_closed_with_attention(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        (checkout / "unexpected.txt").write_text("must be preserved\n", encoding="utf-8")
        plane = resolve(self.root)
        transaction_path = plane.state_root / f"transactions/active/{transaction_id}.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["status"] = "initializing"
        transaction["initialization_rollback_pending"] = True
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        claim_path = plane.state_root / "claims/isolated.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"status": "pending-arbitration", "transaction_id": None})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rollback-pending")
        retained = json.loads(transaction_path.read_text(encoding="utf-8"))
        self.assertEqual(retained["status"], "initialization-needs-attention")
        self.assertTrue((checkout / "unexpected.txt").exists())

    def test_unmaterialized_transaction_intent_reconciles_without_git_cleanup(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        git(self.root, "worktree", "remove", "--force", str(checkout))
        git(self.root, "branch", "-D", str(started["branch"]))
        plane = resolve(self.root)
        transaction_path = plane.state_root / f"transactions/active/{transaction_id}.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["status"] = "initializing"
        transaction.pop("resources_materialized_at", None)
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        claim_path = plane.state_root / "claims/isolated.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"status": "pending-arbitration", "transaction_id": None})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(result["aborts"][0]["action"], "initialization-rolled-back")
        self.assertFalse(transaction_path.exists())

    def test_cleanup_reconcile_repairs_missing_authorization_and_avoids_duplicate_terminal(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        original_emit = transaction_cleanup.emit

        def fail_authorization(*args: object, **kwargs: object):
            if len(args) > 1 and args[1] == "cleanup-authorized":
                raise RuntimeError("injected authorization event failure")
            return original_emit(*args, **kwargs)

        with mock.patch.object(transaction_cleanup, "emit", side_effect=fail_authorization):
            with self.assertRaisesRegex(RuntimeError, "authorization event failure"):
                transactions.abort(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    reason_code="audit-gap",
                    reason="exercise authorization event recovery",
                    discard=True,
                )
        original_replace = transaction_cleanup.os.replace
        failed_once = False

        def fail_archive_once(source: object, destination: object) -> None:
            nonlocal failed_once
            source_path = Path(str(source))
            if (
                not failed_once
                and source_path.name == f"{transaction_id}.json"
                and source_path.parent.name == "active"
                and source_path.parent.parent.name == "cleanups"
            ):
                failed_once = True
                raise OSError("injected cleanup archive failure")
            original_replace(source, destination)

        with mock.patch.object(transaction_cleanup.os, "replace", side_effect=fail_archive_once):
            first = transactions.reconcile(
                self.root,
                steward="agent-a",
                steward_run_id="run-a",
            )
        self.assertEqual(first["aborts"][0]["result"]["cleanup"]["status"], "archive-pending")
        transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sum(
                event.get("event") == "cleanup-authorized"
                and event.get("cleanup_id") == transaction_id
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event.get("event") == "cleanup-completed"
                and event.get("cleanup_id") == transaction_id
                for event in events
            ),
            1,
        )

    def test_changed_discard_requires_fresh_exact_owner_authorization(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        original = transaction_cleanup._run_git

        def fail_remove(root: Path, *arguments: str, **kwargs: object):
            if arguments[:2] == ("worktree", "remove"):
                raise RuntimeError("injected worktree removal failure")
            return original(root, *arguments, **kwargs)

        with mock.patch.object(transaction_cleanup, "_run_git", side_effect=fail_remove):
            aborted = transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="test-attention",
                reason="exercise changed discard protection",
                discard=True,
            )
        self.assertEqual(aborted["cleanup"]["status"], "needs-attention")
        (checkout / "new-note.txt").write_text("new owner evidence\n", encoding="utf-8")
        attention = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )["cleanups"][0]
        self.assertEqual(attention["status"], "needs-attention")
        self.assertIn("changed after authorization", str(attention["error"]))
        transactions.authorize_cleanup(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            reason="Owner inspected the newly added note and approved discard.",
        )
        completed = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )["cleanups"][0]
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(checkout.exists())

    def test_changed_discard_revision_event_is_required_before_cleanup(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        original_git = transaction_cleanup._run_git

        def fail_remove(root: Path, *arguments: str, **kwargs: object):
            if arguments[:2] == ("worktree", "remove"):
                raise RuntimeError("injected cleanup pause")
            return original_git(root, *arguments, **kwargs)

        with mock.patch.object(transaction_cleanup, "_run_git", side_effect=fail_remove):
            transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="reauthorize",
                reason="Create a cleanup that needs fresh authorization.",
                discard=True,
            )
        (checkout / "changed.txt").write_text("changed after authorization\n", encoding="utf-8")
        transactions.reconcile(self.root, steward="agent-a", steward_run_id="run-a")
        original_emit = transaction_cleanup.emit

        def fail_revision_two(*args: object, **kwargs: object):
            payload = kwargs.get("payload")
            if (
                len(args) > 1
                and args[1] == "cleanup-authorized"
                and isinstance(payload, dict)
                and payload.get("authorization_revision") == 2
            ):
                raise RuntimeError("injected revision-two event gap")
            return original_emit(*args, **kwargs)

        with mock.patch.object(transaction_cleanup, "emit", side_effect=fail_revision_two):
            with self.assertRaisesRegex(RuntimeError, "revision-two"):
                transactions.authorize_cleanup(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    reason="Fresh review of changed discard contents.",
                )
        completed = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )["cleanups"][0]
        self.assertEqual(completed["status"], "completed")
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        revisions = sorted(
            event["authorization_revision"]
            for event in events
            if event.get("event") == "cleanup-authorized"
            and event.get("cleanup_id") == transaction_id
        )
        self.assertEqual(revisions, [1, 2])

    def test_transaction_owner_run_is_fenced_and_recovered_exactly(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="abandoned",
            summary="transaction worker stopped",
            force_terminal=True,
            reason_code="process-lost",
        )
        with self.assertRaisesRegex(ValueError, "Run is not active"):
            transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="stale-worker",
                reason="A closed Run cannot authorize discard.",
                discard=True,
            )

        join_run(self.root, run_id="run-b2", owner="agent-b", task="recover transaction")
        recover_run_authority(
            self.root,
            closed_run_id="run-b",
            owner="agent-b",
            recovery_run_id="run-b2",
            evidence="The same owner inspected the isolated checkout and resumed the exact task.",
        )
        aborted = transactions.abort(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b2",
            reason_code="recovered-discard",
            reason="Recovered worker explicitly discards the isolated candidate.",
            discard=True,
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertEqual(aborted["cleanup"]["status"], "completed")

    def test_cleanup_rejects_checkout_reused_by_another_branch(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        moved = checkout.parent / f"moved-{transaction_id}"
        with mock.patch.object(
            transaction_cleanup,
            "reconcile_one",
            return_value={"status": "planned", "cleanup_id": transaction_id},
        ):
            transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="identity-regression",
                reason="retain cleanup intent for registry replacement",
                discard=True,
            )
        git(self.root, "worktree", "move", str(checkout), str(moved))
        git(
            self.root,
            "worktree",
            "add",
            "-b",
            f"replacement-{transaction_id}",
            str(checkout),
            str(started["base_revision"]),
        )

        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )["cleanups"][0]
        self.assertEqual(result["status"], "needs-attention")
        self.assertIn("exact transaction branch", str(result["error"]))
        self.assertTrue(checkout.exists())
        self.assertTrue(moved.exists())

    def test_cleanup_rejects_managed_checkout_symlink_drift(self) -> None:
        started = self._begin()
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        moved = checkout.parent / f"moved-{transaction_id}"
        with mock.patch.object(
            transaction_cleanup,
            "reconcile_one",
            return_value={"status": "planned", "cleanup_id": transaction_id},
        ):
            transactions.abort(
                self.root,
                transaction_id=transaction_id,
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="symlink-regression",
                reason="retain cleanup intent before path drift",
                discard=True,
            )
        git(self.root, "worktree", "move", str(checkout), str(moved))
        checkout.symlink_to(moved, target_is_directory=True)

        result = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )["cleanups"][0]
        self.assertEqual(result["status"], "needs-attention")
        self.assertIn("exact managed transaction path", str(result["error"]))
        self.assertTrue(checkout.is_symlink())
        self.assertTrue(moved.exists())
        diagnosis = transactions.doctor(self.root)
        self.assertEqual(
            [item["transaction_id"] for item in diagnosis["invalid_checkout_records"]],
            [transaction_id],
        )

    def test_doctor_reports_atomic_temp_residue_without_deleting_it(self) -> None:
        plane = resolve(self.root)
        residue = plane.state_root / "events/.event.json.123.456.tmp"
        residue.write_text("durable crash residue\n", encoding="utf-8")
        report = transactions.doctor(self.root)
        self.assertEqual(report["atomic_temp_residues"], [str(residue)])
        self.assertTrue(residue.exists())
