from __future__ import annotations

import json

from dev_mesh_coord import canonical_git
from dev_mesh_coord import contention
from dev_mesh_coord.cli_output import MAX_COMPACT_ITEMS, project
from dev_mesh_coord.control_plane import initialize
from dev_mesh_coord.lifecycle import create_claim, join_run

from helpers import GitWorkspaceTest


class CliOutputTest(GitWorkspaceTest):
    def test_direct_commit_projection_is_small_and_action_oriented(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="direct")
        create_claim(
            self.root,
            scope="direct",
            owner="agent-a",
            run_id="run-a",
            task="edit app",
            paths=["app.txt"],
        )
        (self.root / "app.txt").write_text("base\ncompact\n", encoding="utf-8")
        complete = canonical_git.commit(
            self.root,
            scope="direct",
            owner="agent-a",
            run_id="run-a",
            summary="compact output",
            validation_evidence="focused checks passed",
        )
        compact = project("direct-commit", complete, verbose=False)
        self.assertIsInstance(compact, dict)
        self.assertEqual(compact["status"], "completed")
        self.assertEqual(compact["next_action"], "release_claim")
        self.assertNotIn("started_event", compact)
        self.assertNotIn("terminal_event", compact)
        self.assertNotIn("validation_evidence", compact)
        full_bytes = len(json.dumps(complete, sort_keys=True).encode())
        compact_bytes = len(json.dumps(compact, sort_keys=True).encode())
        self.assertLess(compact_bytes, full_bytes * 0.35)
        self.assertEqual(project("direct-commit", complete, verbose=True), complete)

    def test_status_is_bounded_and_filters_exact_owner_run(self) -> None:
        runs = [
            {
                "run_id": f"run-{index}",
                "owner": f"agent-{index}",
                "status": "active",
                "task": "large detail omitted",
            }
            for index in range(MAX_COMPACT_ITEMS * 2 + 5)
        ]
        claims = [
            {
                "scope": f"scope-{index}",
                "owner": f"agent-{index}",
                "run_id": f"run-{index}",
                "status": "pending-arbitration" if index % 2 else "active",
                "paths": [f"large/{index}/detail.txt"],
            }
            for index in range(MAX_COMPACT_ITEMS * 2 + 5)
        ]
        status = {
            "protocol": "20260814.1",
            "runs": runs,
            "claims": claims,
            "blockers": {"run-1": [{"kind": "claim", "id": "scope-1"}]},
        }
        overview = project("status", status, verbose=False)
        self.assertEqual(overview["counts"]["runs"], len(runs))
        self.assertNotIn("runs", overview)
        self.assertTrue(overview["action_required"]["truncated"])
        self.assertEqual(len(overview["action_required"]["sample"]), MAX_COMPACT_ITEMS)
        self.assertEqual(overview["action_required"]["sample"][0]["kind"], "claim")
        self.assertEqual(overview["counts"]["leave_blocked_runs"], 1)
        self.assertEqual(overview["leave_constraints"]["sample"][0]["kind"], "run")
        self.assertEqual(
            overview["leave_constraints"]["sample"][0]["blocker_count"], 1
        )

        filtered = project(
            "status",
            status,
            verbose=False,
            owner="agent-1",
            run_id="run-1",
            scopes=["scope-1"],
        )
        self.assertEqual(filtered["counts"]["runs"], 1)
        self.assertEqual(filtered["runs"]["sample"][0]["run_id"], "run-1")
        self.assertEqual(filtered["claims"]["sample"][0]["scope"], "scope-1")
        self.assertNotIn("paths", filtered["claims"]["sample"][0])

        full = project(
            "status",
            status,
            verbose=True,
            owner="agent-1",
            run_id="run-1",
            scopes=["scope-1"],
        )
        self.assertEqual(full["runs"], [runs[1]])
        self.assertEqual(full["claims"], [claims[1]])
        self.assertEqual(full["blockers"], {"run-1": status["blockers"]["run-1"]})

        scoped = project(
            "status",
            status,
            verbose=False,
            scopes=["scope-3"],
        )
        self.assertEqual(scoped["counts"]["runs"], 1)
        self.assertEqual(scoped["runs"]["sample"][0]["run_id"], "run-3")
        self.assertEqual(scoped["claims"]["sample"][0]["scope"], "scope-3")

    def test_contention_projection_keeps_bounded_routing_facts(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="active")
        join_run(self.root, run_id="run-b", owner="agent-b", task="pending")
        create_claim(
            self.root,
            scope="active",
            owner="agent-a",
            run_id="run-a",
            task="edit app",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="pending",
            owner="agent-b",
            run_id="run-b",
            task="overlap app",
            paths=["app.txt"],
            allow_overlap=True,
        )
        record = contention.open_for_claim(self.root, scope="pending")
        compact = project("contention-open", record, verbose=False)
        self.assertEqual(compact["contention_id"], pending["contention_id"])
        self.assertEqual(compact["coordinator"]["owner"], "agent-b")
        self.assertEqual(compact["coordinator"]["run_id"], "run-b")
        self.assertEqual(compact["coordinator"]["epoch"], 1)
        self.assertEqual(compact["participants"]["count"], 2)
        self.assertEqual(compact["next_action"], "coordinator_proposes_bounded_decision")
        self.assertNotIn("opened_event", compact)

        waiting = project(
            "contention-wait",
            contention.select_wait(
                self.root,
                contention_id=str(record["contention_id"]),
                scope="pending",
                owner="agent-b",
                run_id="run-b",
                reason="active edit is short",
            ),
            verbose=False,
        )
        self.assertEqual(
            waiting["next_action"], "wait_for_overlap_release_then_activate_claim"
        )
        self.assertEqual(waiting["write_authority"], "none")

    def test_pending_baseline_projection_names_the_only_digest_to_accept(self) -> None:
        compact = project(
            "claim",
            {
                "scope": "continued",
                "owner": "agent-b",
                "run_id": "run-b",
                "status": "pending-baseline",
                "baseline": {
                    "baseline_sha256": "accepted-digest",
                    "actual_paths_sha256": "diagnostic-digest",
                    "evidence_sha256": "evidence-digest",
                },
            },
            verbose=False,
        )
        self.assertEqual(compact["write_authority"], "none")
        self.assertEqual(
            compact["required_action"],
            "inspect_declared_paths_then_accept_exact_baseline",
        )
        self.assertEqual(compact["accept_baseline_sha256"], "accepted-digest")

        changed = project(
            "claim-baseline-accept",
            {
                "scope": "continued",
                "owner": "agent-b",
                "run_id": "run-b",
                "status": "pending-baseline",
                "baseline_changed": True,
                "baseline_accepted": False,
                "baseline": {"baseline_sha256": "new-digest"},
            },
            verbose=False,
        )
        self.assertTrue(changed["retry_required"])
        self.assertEqual(
            changed["next_action"], "review_changed_baseline_then_retry_accept"
        )

    def test_send_output_states_that_delivery_is_external(self) -> None:
        compact = project(
            "send",
            {
                "message_id": "message-1",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
                "requires_ack": True,
            },
            verbose=False,
        )

        self.assertEqual(compact["dev_mesh_effect"], "record_persisted")
        self.assertEqual(
            compact["external_task_delivery"],
            "not_performed_by_dev_mesh",
        )
        self.assertFalse(compact["target_task_woken_by_dev_mesh"])
        self.assertEqual(
            compact["next_action"],
            "ensure_actual_task_delivery_then_wait_for_acknowledgement",
        )

        notice = project(
            "send",
            {
                "message_id": "message-2",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
                "requires_ack": False,
            },
            verbose=False,
        )
        self.assertEqual(notice["next_action"], "ensure_actual_task_delivery")

        verbose = project(
            "send",
            {
                "message_id": "message-3",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
                "requires_ack": True,
                "body": "bounded checkpoint",
            },
            verbose=True,
        )
        self.assertEqual(verbose["dev_mesh_effect"], "record_persisted")
        self.assertEqual(
            verbose["next_action"],
            "ensure_actual_task_delivery_then_wait_for_acknowledgement",
        )

    def test_pause_projection_remains_a_blocking_state(self) -> None:
        blocked = project(
            "claim-pause",
            {
                "scope": "blocked",
                "status": "paused",
                "pause": {"blocker_kind": "dependency"},
            },
            verbose=False,
        )
        self.assertEqual(blocked["next_action"], "wait_for_resume_condition")
