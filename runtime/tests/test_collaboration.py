from __future__ import annotations

import json
from unittest import mock

from dev_mesh_coord import contention, interactions, work, work_results
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    activate_pending_claim,
    create_claim,
    join_run,
    leave_run,
    release_claim,
)

from helpers import GitWorkspaceTest


class CollaborationTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
            semantic_writes=["router"],
        )

    def _overlap(self) -> tuple[dict[str, object], dict[str, object]]:
        pending = create_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            task="parallel",
            paths=["app.txt"],
            semantic_writes=["router"],
        )
        plane = resolve(self.root)
        conflicts = list((plane.state_root / "contentions/active").glob("*.json"))
        self.assertEqual(len(conflicts), 1)
        opened = contention.open_for_claim(self.root, scope="parallel")
        return pending, opened

    def test_overlap_is_pending_and_opens_one_idempotent_contention(self) -> None:
        pending, opened = self._overlap()
        self.assertEqual(pending["status"], "pending-arbitration")
        self.assertEqual(pending["contention_id"], opened["contention_id"])
        again = contention.open_for_claim(self.root, scope="parallel")
        self.assertEqual(again["contention_id"], opened["contention_id"])
        join_run(self.root, run_id="run-c", owner="agent-c", task="third contender")
        with self.assertRaisesRegex(ValueError, "in-flight arbitration"):
            create_claim(
                self.root,
                scope="third",
                owner="agent-c",
                run_id="run-c",
                task="third contender",
                paths=["app.txt"],
                allow_overlap=True,
            )

        with self.assertRaisesRegex(ValueError, "not semantically independent"):
            contention.propose(
                self.root,
                contention_id=str(opened["contention_id"]),
                owner="agent-b",
                run_id="run-b",
                epoch=1,
                decision="parallel-tx",
                reason="unsafe parallel attempt",
            )
        proposed = contention.propose(
            self.root,
            contention_id=str(opened["contention_id"]),
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="exclusive",
            reason="semantic writes overlap",
        )
        revision = int(proposed["decision_revision"])
        contention.respond(self.root, contention_id=str(opened["contention_id"]), scope="primary", owner="agent-a", run_id="run-a", revision=revision, accept=True)
        contention.respond(self.root, contention_id=str(opened["contention_id"]), scope="parallel", owner="agent-b", run_id="run-b", revision=revision, accept=True)
        completed = contention.enact(
            self.root,
            contention_id=str(opened["contention_id"]),
            owner="agent-b",
            run_id="run-b",
            epoch=1,
        )
        self.assertEqual(completed["status"], "completed")
        release_claim(self.root, scope="primary", owner="agent-a", run_id="run-a", summary="wait complete")
        active = activate_pending_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            evidence="original writer released",
        )
        self.assertEqual(active["status"], "active")

    def test_pending_participant_selects_wait_without_shared_consensus(self) -> None:
        pending, opened = self._overlap()
        completed = contention.select_wait(
            self.root,
            contention_id=str(opened["contention_id"]),
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            reason="primary change is short",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["decision"], "wait")
        with self.assertRaisesRegex(ValueError, "still overlaps"):
            activate_pending_claim(
                self.root,
                scope="parallel",
                owner="agent-b",
                run_id="run-b",
            )

        (self.root / "app.txt").write_text("base\nprimary complete\n", encoding="utf-8")
        work_results.complete_claim(
            self.root,
            result_id="primary-result",
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="primary complete",
            validation_evidence="focused checks passed",
        )
        continued = activate_pending_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
        )
        self.assertEqual(continued["status"], "pending-baseline")
        accepted = work_results.accept_baseline(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=str(continued["baseline"]["baseline_sha256"]),
        )
        self.assertEqual(accepted["status"], "active")

        events = [
            json.loads(path.read_text())
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sum(event["event"] == "contention-completed" for event in events), 1
        )
        self.assertFalse(
            any(event["event"] == "contention-decision-proposed" for event in events)
        )
        self.assertFalse(
            any(event["event"] == "contention-decision-responded" for event in events)
        )
        retried = contention.select_wait(
            self.root,
            contention_id=str(opened["contention_id"]),
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            reason="primary change is short",
        )
        self.assertFalse(retried["event_emitted"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            contention.select_wait(
                self.root,
                contention_id=str(opened["contention_id"]),
                scope="parallel",
                owner="agent-b",
                run_id="run-b",
                reason="a different retry reason",
            )

    def test_branch_offload_requires_semantic_resources_from_every_claim(self) -> None:
        join_run(self.root, run_id="run-c", owner="agent-c", task="unclassified overlap")
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="replace with unclassified authority",
        )
        create_claim(
            self.root,
            scope="unclassified-primary",
            owner="agent-a",
            run_id="run-a",
            task="unclassified primary",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="unclassified-pending",
            owner="agent-c",
            run_id="run-c",
            task="unclassified pending",
            paths=["app.txt"],
        )
        with self.assertRaisesRegex(ValueError, "requires semantic writes"):
            contention.propose(
                self.root,
                contention_id=str(pending["contention_id"]),
                owner="agent-c",
                run_id="run-c",
                epoch=1,
                decision="parallel-tx",
                reason="missing semantic evidence must fail closed",
            )

    def test_contention_open_repairs_snapshot_event_and_claim_correlation_gaps(self) -> None:
        with mock.patch.object(
            contention,
            "write_event",
            side_effect=RuntimeError("injected open event gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "open event gap"):
                create_claim(
                    self.root,
                    scope="parallel",
                    owner="agent-b",
                    run_id="run-b",
                    task="parallel",
                    paths=["app.txt"],
                    semantic_writes=["router"],
                    allow_overlap=True,
                )
        plane = resolve(self.root)
        claim_path = plane.state_root / "claims/parallel.json"
        self.assertIsNone(json.loads(claim_path.read_text()).get("contention_id"))
        repaired = contention.open_for_claim(self.root, scope="parallel")
        self.assertEqual(
            json.loads(claim_path.read_text())["contention_id"],
            repaired["contention_id"],
        )
        events = [
            json.loads(path.read_text())
            for path in (plane.state_root / "events").glob("*.json")
        ]
        self.assertEqual(
            sum(
                event.get("event") == "contention-opened"
                and event.get("contention_id") == repaired["contention_id"]
                for event in events
            ),
            1,
        )

        release_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            summary="restart the second open window",
        )
        # Close the first slice before requesting the same overlap again.
        contention.cancel(
            self.root,
            contention_id=str(repaired["contention_id"]),
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            reason_code="test-restart",
            reason="Exercise the event-before-Claim correlation window.",
        )
        real_replace = contention.replace_json
        calls = 0

        def fail_claim_correlation(*arguments: object, **keywords: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected Claim correlation gap")
            real_replace(*arguments, **keywords)

        with mock.patch.object(
            contention,
            "replace_json",
            side_effect=fail_claim_correlation,
        ):
            with self.assertRaisesRegex(RuntimeError, "Claim correlation gap"):
                create_claim(
                    self.root,
                    scope="parallel-two",
                    owner="agent-b",
                    run_id="run-b",
                    task="parallel again",
                    paths=["app.txt"],
                    semantic_writes=["router"],
                    allow_overlap=True,
                )
        repaired_again = contention.open_for_claim(self.root, scope="parallel-two")
        self.assertEqual(
            json.loads((plane.state_root / "claims/parallel-two.json").read_text())[
                "contention_id"
            ],
            repaired_again["contention_id"],
        )

    def test_request_is_not_handoff_and_handoff_has_terminal_state(self) -> None:
        request = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            subject="please inspect",
            body="No authority transfer.",
            interaction_kind="request",
            requires_ack=True,
            source_run_id="run-a",
        )
        ack = interactions.acknowledge(
            self.root,
            message_id=str(request["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
            note="seen",
        )
        self.assertIsNone(request["handoff_id"])
        self.assertEqual(ack["owner"], "agent-b")

        offered = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            subject="take checkpoint",
            body="Continue from the validated checkpoint.",
            interaction_kind="handoff",
            topic="takeover",
            requires_ack=True,
            source_run_id="run-a",
            handoff_id="checkpoint-offer",
        )
        interactions.acknowledge(
            self.root,
            message_id=str(offered["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
        )
        handoff = resolve(self.root).state_root / "handoffs" / f"{offered['handoff_id']}.json"
        self.assertIn('"status": "accepted"', handoff.read_text())

    def test_handoff_reject_and_withdraw_require_exact_runs(self) -> None:
        rejected_offer = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            source_run_id="run-a",
            subject="offer one",
            body="checkpoint one",
            interaction_kind="handoff",
            requires_ack=True,
            handoff_id="rejected-offer",
        )
        rejected = interactions.reject(
            self.root,
            handoff_id=str(rejected_offer["handoff_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
            reason_code="capacity",
            reason="busy",
        )
        self.assertEqual(rejected["status"], "rejected")

        withdrawn_offer = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            source_run_id="run-a",
            subject="offer two",
            body="checkpoint two",
            interaction_kind="handoff",
            requires_ack=True,
            handoff_id="withdrawn-offer",
        )
        withdrawn = interactions.withdraw(
            self.root,
            handoff_id=str(withdrawn_offer["handoff_id"]),
            source_owner="agent-a",
            source_run_id="run-a",
            reason_code="superseded",
            reason="work changed",
        )
        self.assertEqual(withdrawn["status"], "withdrawn")

    def test_handoff_requires_caller_supplied_stable_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "caller-supplied stable handoff id"):
            interactions.send(
                self.root,
                source_owner="agent-a",
                target_owner="agent-b",
                source_run_id="run-a",
                subject="unstable offer",
                body="cannot be retried after a crash",
                interaction_kind="handoff",
                requires_ack=True,
            )

        notice = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            source_run_id="run-a",
            subject="inert notice",
            body="no handoff identity required",
            interaction_kind="notice",
        )
        self.assertIsNone(notice["handoff_id"])

    def test_explicit_handoff_retry_repairs_message_first_construction(self) -> None:
        real_write = interactions.write_json_exclusive
        calls = 0

        def fail_handoff_snapshot(*arguments: object, **keywords: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected handoff snapshot gap")
            real_write(*arguments, **keywords)

        with mock.patch.object(
            interactions,
            "write_json_exclusive",
            side_effect=fail_handoff_snapshot,
        ):
            with self.assertRaisesRegex(RuntimeError, "handoff snapshot gap"):
                interactions.send(
                    self.root,
                    source_owner="agent-a",
                    target_owner="agent-b",
                    source_run_id="run-a",
                    subject="stable offer",
                    body="bounded checkpoint",
                    interaction_kind="handoff",
                    requires_ack=True,
                    handoff_id="stable-offer",
                )
        plane = resolve(self.root)
        self.assertEqual(len(list((plane.state_root / "messages").glob("*.json"))), 1)
        self.assertEqual(list((plane.state_root / "handoffs").glob("*.json")), [])
        repaired = interactions.send(
            self.root,
            source_owner="agent-a",
            target_owner="agent-b",
            source_run_id="run-a",
            subject="stable offer",
            body="bounded checkpoint",
            interaction_kind="handoff",
            requires_ack=True,
            handoff_id="stable-offer",
        )
        self.assertEqual(repaired["handoff_id"], "stable-offer")
        handoff = json.loads(
            (plane.state_root / "handoffs/stable-offer.json").read_text()
        )
        self.assertEqual(handoff["message_id"], repaired["message_id"])
        events = [
            json.loads(path.read_text())
            for path in (plane.state_root / "events").glob("*.json")
        ]
        self.assertEqual(
            sum(
                event.get("event") == "handoff-offered"
                and event.get("handoff_id") == "stable-offer"
                for event in events
            ),
            1,
        )

    def test_waiting_and_diverted_work_are_bounded_intervals(self) -> None:
        waiting = work.suspend(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            disposition="waiting",
            reason="dependency",
            blocked_by_owner="agent-b",
        )
        resumed = work.resume(
            self.root,
            work_state_id=str(waiting["work_state_id"]),
            owner="agent-a",
            run_id="run-a",
            evidence="dependency cleared",
        )
        self.assertEqual(resumed["status"], "resumed")

    def test_work_resume_retries_one_durable_terminal_event(self) -> None:
        waiting = work.suspend(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            disposition="waiting",
            reason="dependency",
        )
        work_state_id = str(waiting["work_state_id"])
        with mock.patch.object(
            work,
            "write_event",
            side_effect=RuntimeError("injected work terminal event gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal event gap"):
                work.resume(
                    self.root,
                    work_state_id=work_state_id,
                    owner="agent-a",
                    run_id="run-a",
                    evidence="dependency cleared",
                )
        with mock.patch.object(
            work.os,
            "replace",
            side_effect=RuntimeError("injected work archive gap"),
        ):
            with self.assertRaisesRegex(RuntimeError, "work archive gap"):
                work.resume(
                    self.root,
                    work_state_id=work_state_id,
                    owner="agent-a",
                    run_id="run-a",
                    evidence="dependency cleared",
                )
        resumed = work.resume(
            self.root,
            work_state_id=work_state_id,
            owner="agent-a",
            run_id="run-a",
            evidence="dependency cleared",
        )
        retried = work.resume(
            self.root,
            work_state_id=work_state_id,
            owner="agent-a",
            run_id="run-a",
            evidence="dependency cleared",
        )
        self.assertEqual(resumed["archive"], retried["archive"])
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

    def test_active_work_blocks_run_leave_and_closed_run_cannot_suspend(self) -> None:
        waiting = work.suspend(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            disposition="waiting",
            reason="dependency",
        )
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="Claim complete while diagnostic wait remains",
        )
        with self.assertRaisesRegex(ValueError, "active coordination"):
            leave_run(
                self.root,
                run_id="run-a",
                owner="agent-a",
                outcome="completed",
                summary="must resume work first",
            )
        work.resume(
            self.root,
            work_state_id=str(waiting["work_state_id"]),
            owner="agent-a",
            run_id="run-a",
            evidence="dependency resolved",
        )
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="work interval closed",
        )
        create_claim(
            self.root,
            scope="closed-run-claim",
            owner="agent-b",
            run_id="run-b",
            task="will stop",
            paths=["other.txt"],
            intent="read",
        )
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="failed",
            summary="worker stopped",
            force_terminal=True,
            reason_code="process-lost",
        )
        with self.assertRaisesRegex(ValueError, "exact active owner Run"):
            work.suspend(
                self.root,
                scope="closed-run-claim",
                owner="agent-b",
                run_id="run-b",
                disposition="waiting",
                reason="must recover first",
            )

    def test_read_claim_does_not_block_a_writer(self) -> None:
        reading = create_claim(
            self.root,
            scope="read-other",
            owner="agent-b",
            run_id="run-b",
            task="inspect other",
            paths=["other.txt"],
            intent="read",
        )
        writing = create_claim(
            self.root,
            scope="write-other",
            owner="agent-a",
            run_id="run-a",
            task="edit other",
            paths=["other.txt"],
        )
        self.assertEqual(reading["status"], "active")
        self.assertEqual(writing["status"], "active")
