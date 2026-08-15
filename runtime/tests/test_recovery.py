from __future__ import annotations

import json
from unittest import mock

from dev_mesh_coord import contention, interactions, work
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    append_audit_correction,
    close_run_after_review,
    create_claim,
    heartbeat_claim,
    join_run,
    leave_run,
    pause_claim,
    preview_reviewed_run_close,
    recover_run_authority,
    release_claim,
    resume_claim,
)

from helpers import GitWorkspaceTest


class AuthorityRecoveryTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)

    def _write_active_direct_commit(
        self, *, direct_commit_id: str, run_id: str, status: str = "staging"
    ) -> None:
        path = (
            resolve(self.root).state_root
            / "direct-commits"
            / "active"
            / f"{direct_commit_id}.json"
        )
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "direct_commit_id": direct_commit_id,
                    "scope": "primary",
                    "owner": "agent-a",
                    "run_id": run_id,
                    "status": status,
                }
            ),
            encoding="utf-8",
        )

    def test_active_direct_commit_blocks_completed_leave(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="direct commit")
        self._write_active_direct_commit(
            direct_commit_id="direct-commit-one", run_id="run-a"
        )

        with self.assertRaisesRegex(ValueError, "active coordination"):
            leave_run(
                self.root,
                run_id="run-a",
                owner="agent-a",
                outcome="completed",
                summary="must reconcile direct commit first",
            )

    def test_reviewed_close_rechecks_exact_run_and_records_operator_evidence(self) -> None:
        join_run(self.root, run_id="run-review", owner="agent-a", task="finished work")
        preview = preview_reviewed_run_close(self.root, run_id="run-review")
        self.assertEqual(preview["allowed_outcomes"], ["abandoned", "completed", "failed"])
        self.assertFalse(preview["authority_preserved"])

        closed = close_run_after_review(
            self.root,
            run_id="run-review",
            review_token=str(preview["review_token"]),
            reviewer="local-operator",
            outcome="completed",
            reason_code="reviewed-complete",
            evidence="Reviewed validation and confirmed the Agent omitted its terminal leave.",
        )

        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["operator_review"]["reviewer"], "local-operator")
        repeated = close_run_after_review(
            self.root,
            run_id="run-review",
            review_token=str(preview["review_token"]),
            reviewer="local-operator",
            outcome="completed",
            reason_code="reviewed-complete",
            evidence="Reviewed validation and confirmed the Agent omitted its terminal leave.",
        )
        self.assertEqual(repeated["left_event_id"], closed["left_event_id"])
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (resolve(self.root).state_root / "events").glob("*.json")
        ]
        terminal = [item for item in events if item["event"] == "agent-left"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["closure_kind"], "operator-reviewed")

    def test_reviewed_close_preserves_authority_and_rejects_stale_preview(self) -> None:
        join_run(self.root, run_id="run-review", owner="agent-a", task="interrupted work")
        clean_preview = preview_reviewed_run_close(self.root, run_id="run-review")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-review",
            task="interrupted work",
            paths=["app.txt"],
        )
        with self.assertRaisesRegex(ValueError, "changed after review"):
            close_run_after_review(
                self.root,
                run_id="run-review",
                review_token=str(clean_preview["review_token"]),
                reviewer="local-operator",
                outcome="abandoned",
                reason_code="reviewed-abandoned",
                evidence="The owner task is no longer running.",
            )

        preview = preview_reviewed_run_close(self.root, run_id="run-review")
        self.assertEqual(preview["allowed_outcomes"], ["failed", "abandoned"])
        self.assertTrue(preview["authority_preserved"])
        heartbeat_claim(self.root, scope="primary", owner="agent-a", run_id="run-review")
        with self.assertRaisesRegex(ValueError, "changed after review"):
            close_run_after_review(
                self.root,
                run_id="run-review",
                review_token=str(preview["review_token"]),
                reviewer="local-operator",
                outcome="abandoned",
                reason_code="reviewed-abandoned",
                evidence="A new heartbeat must invalidate the review.",
            )
        preview = preview_reviewed_run_close(self.root, run_id="run-review")
        with self.assertRaisesRegex(ValueError, "cannot be closed as completed"):
            close_run_after_review(
                self.root,
                run_id="run-review",
                review_token=str(preview["review_token"]),
                reviewer="local-operator",
                outcome="completed",
                reason_code="reviewed-complete",
                evidence="Unsafe completed close must fail.",
            )
        closed = close_run_after_review(
            self.root,
            run_id="run-review",
            review_token=str(preview["review_token"]),
            reviewer="local-operator",
            outcome="abandoned",
            reason_code="reviewed-abandoned",
            evidence="The task stopped; preserve its Claim for same-owner recovery.",
        )
        self.assertTrue(closed["operator_review"]["authority_preserved"])
        claim = json.loads(
            (resolve(self.root).state_root / "claims/primary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claim["status"], "active")

    def test_authority_recovery_preserves_direct_commit_actor_for_reconcile(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="direct commit")
        self._write_active_direct_commit(
            direct_commit_id="direct-commit-one",
            run_id="run-old",
            status="committing",
        )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="process lost during canonical mutation",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover authority")

        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="Canonical reconcile retains the exact fenced actor identity.",
        )

        self.assertEqual(recovered["authority_recovered_to"], "run-new")
        snapshot = json.loads(
            (
                resolve(self.root).state_root
                / "direct-commits/active/direct-commit-one.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["owner"], "agent-a")
        self.assertEqual(snapshot["run_id"], "run-old")

    def test_malformed_direct_commit_fails_recovery_before_claim_rebind(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="primary")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            task="primary",
            paths=["app.txt"],
        )
        self._write_active_direct_commit(
            direct_commit_id="direct-commit-one",
            run_id="run-old",
            status="unknown",
        )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="process lost",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover authority")

        with self.assertRaisesRegex(ValueError, "direct commit status is malformed"):
            recover_run_authority(
                self.root,
                closed_run_id="run-old",
                owner="agent-a",
                recovery_run_id="run-new",
                evidence="Malformed durable intent must fail the complete preflight.",
            )

        claim = json.loads(
            (resolve(self.root).state_root / "claims/primary.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(claim["run_id"], "run-old")

    def test_failed_run_rebinds_stranded_claim_to_same_owner_continuity(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="primary")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            task="primary",
            paths=["app.txt"],
        )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="abandoned",
            summary="process lost",
            force_terminal=True,
            reason_code="process-lost",
        )
        with self.assertRaisesRegex(ValueError, "not active"):
            release_claim(
                self.root,
                scope="primary",
                owner="agent-a",
                run_id="run-old",
                summary="cannot use closed Run",
            )
        join_run(self.root, run_id="run-recovery", owner="agent-a", task="recover primary")
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-recovery",
            evidence="Same task owner inspected the clean Claim and resumed continuity.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-recovery")
        claim = json.loads((resolve(self.root).state_root / "claims/primary.json").read_text())
        self.assertEqual(claim["run_id"], "run-recovery")
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-recovery",
            summary="recovered and released",
        )

    def test_authority_recovery_reconciles_partial_object_and_run_updates(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="primary")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            task="primary",
            paths=["app.txt"],
        )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="interrupted during authority recovery",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover primary")
        plane = resolve(self.root)
        claim_path = plane.state_root / "claims/primary.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"run_id": "run-new", "previous_run_id": "run-old"})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")

        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="Recovered after the Claim snapshot advanced ahead of the Run evidence.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-new")
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in plane.state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sum(event.get("event") == "run-authority-recovered" for event in events),
            1,
        )

        retried = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="Idempotent retry observes the completed recovery.",
        )
        self.assertEqual(retried["authority_recovered_to"], "run-new")
        recovery = json.loads(
            (plane.state_root / "runs/run-new.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("recovered_authority_from", recovery)

    def test_authority_recovery_finishes_after_rebound_claim_was_archived(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="primary")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            task="primary",
            paths=["app.txt"],
            intent="read",
        )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="recovery metadata lagged",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover primary")
        plane = resolve(self.root)
        claim_path = plane.state_root / "claims/primary.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update({"run_id": "run-new", "previous_run_id": "run-old"})
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-new",
            summary="rebound work finished before recovery metadata",
        )
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="Archived rebound Claim proves the same-owner continuity.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-new")

    def test_closed_coordinator_is_fenced_then_recovered_and_cancelled(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="overlap")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="overlap",
            owner="agent-b",
            run_id="run-b",
            task="overlap",
            paths=["app.txt"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="failed",
            summary="coordinator process ended",
            force_terminal=True,
            reason_code="process-lost",
        )
        with self.assertRaisesRegex(ValueError, "Run is not active"):
            contention.propose(
                self.root,
                contention_id=contention_id,
                owner="agent-b",
                run_id="run-b",
                epoch=1,
                decision="exclusive",
                reason="must be fenced",
            )
        join_run(self.root, run_id="run-b2", owner="agent-b", task="recover overlap")
        recover_run_authority(
            self.root,
            closed_run_id="run-b",
            owner="agent-b",
            recovery_run_id="run-b2",
            evidence="Recovered exact same-owner Claim and contention snapshot.",
        )
        cancelled = contention.cancel(
            self.root,
            contention_id=contention_id,
            scope="overlap",
            owner="agent-b",
            run_id="run-b2",
            reason_code="task-decomposed",
            reason="Retry later as a non-overlapping slice.",
        )
        self.assertEqual(cancelled["status"], "cancelled")

        terminal_path = next(
            path
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("event")
            == "contention-cancelled"
        )
        terminal_path.unlink()
        reconciled = contention.reconcile(self.root)
        self.assertEqual(
            reconciled,
            [{"contention_id": contention_id, "action": "terminal-event-appended"}],
        )
        release_claim(
            self.root,
            scope="overlap",
            owner="agent-b",
            run_id="run-b2",
            summary="cancelled request",
        )

    def test_contention_terminal_intent_fences_mutation_and_reconciles_exact_event(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="overlap")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="overlap",
            owner="agent-b",
            run_id="run-b",
            task="overlap",
            paths=["app.txt"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="exclusive",
            reason="bounded wait",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (
            ("primary", "agent-a", "run-a"),
            ("overlap", "agent-b", "run-b"),
        ):
            contention.respond(
                self.root,
                contention_id=contention_id,
                scope=scope,
                owner=owner,
                run_id=run_id,
                revision=revision,
                accept=True,
            )
        with mock.patch.object(
            contention,
            "write_event",
            side_effect=RuntimeError("injected stop after terminal intent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal intent"):
                contention.enact(
                    self.root,
                    contention_id=contention_id,
                    owner="agent-b",
                    run_id="run-b",
                    epoch=1,
                )
        with self.assertRaisesRegex(ValueError, "terminal intent"):
            contention.cancel(
                self.root,
                contention_id=contention_id,
                scope="primary",
                owner="agent-a",
                run_id="run-a",
                reason_code="must-not-double-terminal",
                reason="A terminal decision is already fenced.",
            )
        updates = contention.reconcile(self.root)
        self.assertEqual(updates[0]["action"], "terminal-intent-completed")
        archive = json.loads(
            resolve(self.root).state_root.joinpath(
                f"contentions/archive/{contention_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(archive["status"], "completed")
        self.assertEqual(archive["decision_revision"], revision)
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        terminals = [
            event
            for event in events
            if event.get("contention_id") == contention_id
            and event.get("event") in {"contention-completed", "contention-cancelled"}
        ]
        self.assertEqual([(item["event"], item["revision"]) for item in terminals], [("contention-completed", revision)])

    def test_recovery_does_not_rewrite_a_fenced_contention_terminal_identity(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="overlap")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="overlap",
            owner="agent-b",
            run_id="run-b",
            task="overlap",
            paths=["app.txt"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="exclusive",
            reason="finish before recovery",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (
            ("primary", "agent-a", "run-a"),
            ("overlap", "agent-b", "run-b"),
        ):
            contention.respond(
                self.root,
                contention_id=contention_id,
                scope=scope,
                owner=owner,
                run_id=run_id,
                revision=revision,
                accept=True,
            )
        with mock.patch.object(
            contention,
            "write_event",
            side_effect=RuntimeError("injected finalizing gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "finalizing gap"):
                contention.enact(
                    self.root,
                    contention_id=contention_id,
                    owner="agent-b",
                    run_id="run-b",
                    epoch=1,
                )
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="failed",
            summary="stopped after terminal intent",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-b2", owner="agent-b", task="recover terminal")
        recover_run_authority(
            self.root,
            closed_run_id="run-b",
            owner="agent-b",
            recovery_run_id="run-b2",
            evidence="Terminal intent is already fenced under the old exact Run.",
        )
        snapshot = json.loads(
            resolve(self.root).state_root.joinpath(
                f"contentions/active/{contention_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["coordinator"]["run_id"], "run-b")
        self.assertEqual(snapshot["terminal_event"]["coordinator_run_id"], "run-b")
        updates = contention.reconcile(self.root)
        self.assertEqual(updates[0]["action"], "terminal-intent-completed")

    def test_recovery_finishes_fenced_work_under_its_original_terminal_run(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="waiting")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            task="waiting",
            paths=["app.txt"],
        )
        waiting = work.suspend(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-old",
            disposition="waiting",
            reason="dependency",
        )
        work_state_id = str(waiting["work_state_id"])
        with mock.patch.object(
            work,
            "write_event",
            side_effect=RuntimeError("injected finalizing work gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "finalizing work gap"):
                work.resume(
                    self.root,
                    work_state_id=work_state_id,
                    owner="agent-a",
                    run_id="run-old",
                    evidence="dependency cleared",
                )
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="stopped after work terminal intent",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover work")
        recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="The old exact Run already fenced the terminal event.",
        )
        resumed = work.resume(
            self.root,
            work_state_id=work_state_id,
            owner="agent-a",
            run_id="run-new",
            evidence="dependency cleared",
        )
        self.assertEqual(resumed["status"], "resumed")
        self.assertEqual(resumed["run_id"], "run-old")
        events = [
            json.loads(path.read_text())
            for path in (resolve(self.root).state_root / "events").glob("*.json")
        ]
        self.assertEqual(
            sum(
                event.get("event") == "work-resumed"
                and event.get("work_state_id") == work_state_id
                for event in events
            ),
            1,
        )

    def test_handoff_terminal_snapshot_prevents_mutually_exclusive_terminal_events(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="source")
        join_run(self.root, run_id="run-b", owner="agent-b", task="target")
        message = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            subject="handoff",
            body="bounded work",
            interaction_kind="handoff",
            source_run_id="run-a",
            requires_ack=True,
            handoff_id="terminal-snapshot-offer",
        )
        handoff_id = str(message["handoff_id"])
        with mock.patch.object(
            interactions,
            "write_event",
            side_effect=RuntimeError("injected terminal event gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "event gap"):
                interactions.reject(
                    self.root,
                    handoff_id=handoff_id,
                    target_owner="agent-b",
                    target_run_id="run-b",
                    reason_code="not-accepted",
                    reason="Target declined the work.",
                )
        with self.assertRaisesRegex(ValueError, "already terminal"):
            interactions.withdraw(
                self.root,
                handoff_id=handoff_id,
                source_owner="agent-a",
                source_run_id="run-a",
                reason_code="too-late",
                reason="Cannot create a second terminal outcome.",
            )
        snapshot = json.loads(
            resolve(self.root).state_root.joinpath(f"handoffs/{handoff_id}.json").read_text()
        )
        self.assertEqual(snapshot["status"], "rejected")

    def test_authority_recovery_retry_accepts_terminal_rebound_handoff_as_proof(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="source")
        join_run(self.root, run_id="run-target", owner="agent-b", task="target")
        message = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            subject="recoverable handoff",
            body="bounded work",
            interaction_kind="handoff",
            source_run_id="run-old",
            requires_ack=True,
            handoff_id="recoverable-offer",
        )
        handoff_id = str(message["handoff_id"])
        leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="stopped during recovery",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover handoff")
        handoff_path = resolve(self.root).state_root / f"handoffs/{handoff_id}.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff.update(
            {
                "source_run_id": "run-new",
                "previous_source_run_id": "run-old",
                "source_recovery_run_lineage": ["run-old"],
            }
        )
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        interactions.acknowledge(
            self.root,
            message_id=str(message["message_id"]),
            target_owner="agent-b",
            target_run_id="run-target",
        )
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-old",
            owner="agent-a",
            recovery_run_id="run-new",
            evidence="Accepted handoff snapshot proves the earlier same-owner rebind.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-new")

    def test_bounded_recovery_lineage_preserves_earlier_partial_recovery_proof(self) -> None:
        join_run(self.root, run_id="run-one", owner="agent-a", task="primary")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-one",
            task="primary",
            paths=["app.txt"],
        )
        leave_run(
            self.root,
            run_id="run-one",
            owner="agent-a",
            outcome="failed",
            summary="first recovery interrupted",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-two", owner="agent-a", task="first recovery")
        claim_path = resolve(self.root).state_root / "claims/primary.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim.update(
            {
                "run_id": "run-two",
                "previous_run_id": "run-one",
                "recovery_run_lineage": ["run-one"],
            }
        )
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        leave_run(
            self.root,
            run_id="run-two",
            owner="agent-a",
            outcome="failed",
            summary="second recovery interrupted",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-three", owner="agent-a", task="final recovery")
        recover_run_authority(
            self.root,
            closed_run_id="run-two",
            owner="agent-a",
            recovery_run_id="run-three",
            evidence="Recover the second interrupted Run.",
        )
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-one",
            owner="agent-a",
            recovery_run_id="run-three",
            evidence="Bounded lineage still proves the first interrupted Run.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-three")
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(claim["recovery_run_lineage"], ["run-one", "run-two"])

    def test_recovery_preflights_every_lineage_before_mutating_any_authority(self) -> None:
        join_run(self.root, run_id="run-old", owner="agent-a", task="two claims")
        create_claim(
            self.root,
            scope="a-first",
            owner="agent-a",
            run_id="run-old",
            task="first",
            paths=["app.txt"],
        )
        create_claim(
            self.root,
            scope="z-overflow",
            owner="agent-a",
            run_id="run-old",
            task="overflow",
            paths=["other.txt"],
        )
        plane = resolve(self.root)
        first_path = plane.state_root / "claims/a-first.json"
        overflow_path = plane.state_root / "claims/z-overflow.json"
        overflow = json.loads(overflow_path.read_text(encoding="utf-8"))
        overflow["recovery_run_lineage"] = [f"prior-{index}" for index in range(64)]
        overflow_path.write_text(json.dumps(overflow), encoding="utf-8")
        closed = leave_run(
            self.root,
            run_id="run-old",
            owner="agent-a",
            outcome="failed",
            summary="lineage bound reached",
            force_terminal=True,
            reason_code="process-lost",
        )
        attention = closed["attention"]
        self.assertIsInstance(attention, dict)
        assert isinstance(attention, dict)
        self.assertEqual(attention["reference_count"], 2)
        self.assertLessEqual(len(attention["reference_sample"]), 32)
        join_run(self.root, run_id="run-new", owner="agent-a", task="recover")
        first_before = first_path.read_bytes()
        overflow_before = overflow_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "64-Run"):
            recover_run_authority(
                self.root,
                closed_run_id="run-old",
                owner="agent-a",
                recovery_run_id="run-new",
                evidence="Must reject before changing the first Claim.",
            )
        self.assertEqual(first_path.read_bytes(), first_before)
        self.assertEqual(overflow_path.read_bytes(), overflow_before)
        old_run = json.loads((plane.state_root / "runs/run-old.json").read_text())
        self.assertNotIn("authority_recovered_to", old_run)
        events = [
            json.loads(path.read_text())
            for path in (plane.state_root / "events").glob("*.json")
        ]
        self.assertFalse(
            any(event.get("event") == "run-authority-recovered" for event in events)
        )

    def test_recovered_participant_replaces_same_reason_response_run(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="overlap")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="overlap",
            owner="agent-b",
            run_id="run-b",
            task="overlap",
            paths=["app.txt"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="exclusive",
            reason="short overlap",
        )
        revision = int(proposed["decision_revision"])
        contention.respond(
            self.root,
            contention_id=contention_id,
            scope="overlap",
            owner="agent-b",
            run_id="run-b",
            revision=revision,
            accept=True,
            reason="accepted",
        )
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="failed",
            summary="response owner stopped",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-b2", owner="agent-b", task="recover overlap")
        recover_run_authority(
            self.root,
            closed_run_id="run-b",
            owner="agent-b",
            recovery_run_id="run-b2",
            evidence="Same owner resumed the exact contention participant.",
        )
        updated = contention.respond(
            self.root,
            contention_id=contention_id,
            scope="overlap",
            owner="agent-b",
            run_id="run-b2",
            revision=revision,
            accept=True,
            reason="accepted",
        )
        self.assertEqual(updated["responses"]["overlap"]["run_id"], "run-b2")

    def test_structured_pause_resume_and_owner_scoped_correction(self) -> None:
        join_run(self.root, run_id="run-a", owner="agent-a", task="publish output")
        created = create_claim(
            self.root,
            scope="canonical-output",
            owner="agent-a",
            run_id="run-a",
            task="publish output",
            paths=["app.txt"],
            semantic_writes=["canonical-output"],
        )
        with self.assertRaisesRegex(ValueError, "requires operation"):
            pause_claim(
                self.root,
                scope="canonical-output",
                owner="agent-a",
                run_id="run-a",
                blocker_kind="authorization",
                checkpoint="candidate ready",
                resume_condition="user authorizes",
            )
        paused = pause_claim(
            self.root,
            scope="canonical-output",
            owner="agent-a",
            run_id="run-a",
            blocker_kind="authorization",
            checkpoint="candidate ready",
            resume_condition="user authorizes and lock state is rechecked",
            operation_name="publish canonical output",
            resources=["canonical-output"],
            error_kind="sandbox-write-denied",
            retain_paths_reason="Resume after exact authorization.",
        )
        self.assertEqual(paused["pause"]["blocker_kind"], "authorization")
        resumed = resume_claim(
            self.root,
            scope="canonical-output",
            owner="agent-a",
            run_id="run-a",
            evidence="Authorized exact operation; lock absent and target unchanged.",
        )
        self.assertEqual(resumed["last_pause"]["error_kind"], "sandbox-write-denied")
        corrected = append_audit_correction(
            self.root,
            scope="canonical-output",
            owner="agent-a",
            run_id="run-a",
            supersedes_event_id=str(created["created_event_id"]),
            observation="Fresh inspection found the earlier base note incomplete.",
            resources=["canonical-output"],
        )
        self.assertEqual(corrected["event"], "audit-correction")
