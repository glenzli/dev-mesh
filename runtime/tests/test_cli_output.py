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
            "protocol": "20260812.1",
            "runs": runs,
            "claims": claims,
            "blockers": {"run-1": [{"kind": "claim", "id": "scope-1"}]},
        }
        overview = project("status", status, verbose=False)
        self.assertEqual(overview["counts"]["runs"], len(runs))
        self.assertNotIn("runs", overview)
        self.assertTrue(overview["action_required"]["truncated"])
        self.assertEqual(len(overview["action_required"]["sample"]), MAX_COMPACT_ITEMS)
        self.assertEqual(overview["action_required"]["sample"][0]["kind"], "run")
        self.assertEqual(overview["action_required"]["sample"][0]["blocker_count"], 1)

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
