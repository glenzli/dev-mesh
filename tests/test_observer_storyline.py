from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.catalog import discover_workspaces  # noqa: E402
from dev_mesh_observer.operations import register_discovery  # noqa: E402
from dev_mesh_observer.store import ObserverStore  # noqa: E402
from dev_mesh_observer.storyline import (  # noqa: E402
    build_collaboration_storyline,
)


class ObserverStorylineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.events = self.workspace / ".agent-coordination" / "events"
        self.active = (
            self.workspace
            / ".agent-coordination"
            / "contentions"
            / "active"
        )
        self.events.mkdir(parents=True)
        self.active.mkdir(parents=True)
        self.data_dir = self.base / "observer-data"
        self._seed_events()
        self._seed_active_contention()
        sources = discover_workspaces([self.workspace], max_depth=0)
        with ObserverStore(self.data_dir) as store:
            register_discovery(store, [self.workspace], sources)
            result = store.collect()
            self.assertEqual(result["inserted"], 13)
            self.workspace_id = str(
                store.connection.execute(
                    "SELECT workspace_id FROM workspaces"
                ).fetchone()[0]
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_event(self, index: int, payload: dict[str, object]) -> None:
        (self.events / f"{index:03d}-{payload['event']}.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _seed_events(self) -> None:
        records = [
            {
                "at": "2026-08-11T00:00:00Z",
                "event": "agent-joined",
                "run_id": "run-a",
                "owner": "agent-a",
                "task_summary": "Build the route",
            },
            {
                "at": "2026-08-11T00:00:10Z",
                "event": "claim-created",
                "scope": "route-a",
                "owner": "agent-a",
                "paths": ["src/shared.py"],
                "semantic_resources": ["route:/a"],
            },
            {
                "at": "2026-08-11T00:00:20Z",
                "event": "agent-joined",
                "run_id": "run-b",
                "owner": "agent-b",
                "task_summary": "Build the provider",
            },
            {
                "at": "2026-08-11T00:00:30Z",
                "event": "claim-created",
                "scope": "provider-b",
                "owner": "agent-b",
                "paths": ["src/shared.py"],
                "semantic_resources": ["provider:b"],
            },
            {
                "at": "2026-08-11T00:01:00Z",
                "event": "contention-opened",
                "contention_id": "contention-1",
                "owner": "agent-b",
                "owners": ["agent-a", "agent-b"],
                "scopes": ["route-a", "provider-b"],
                "paths": ["src/shared.py"],
                "recommendation": "parallel-tx",
                "reason": "declared semantic writes are disjoint",
            },
            {
                "at": "2026-08-11T00:01:10Z",
                "event": "contention-decision-proposed",
                "contention_id": "contention-1",
                "decision_revision": 1,
                "mode": "parallel-tx",
                "owners": ["agent-a", "agent-b"],
                "scopes": ["route-a", "provider-b"],
            },
            {
                "at": "2026-08-11T00:01:20Z",
                "event": "handoff-offered",
                "handoff_id": "handoff-1",
                "run_id": "run-a",
                "source_run_id": "run-a",
                "owner": "agent-a",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
            },
            {
                "at": "2026-08-11T00:01:30Z",
                "event": "handoff-accepted",
                "handoff_id": "handoff-1",
                "run_id": "run-b",
                "source_run_id": "run-a",
                "target_run_id": "run-b",
                "owner": "agent-b",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
            },
            {
                "at": "2026-08-11T00:02:00Z",
                "event": "transaction-activated",
                "transaction_id": "tx-1",
            },
            {
                "at": "2026-08-11T00:02:10Z",
                "event": "transaction-prepared",
                "transaction_id": "tx-1",
                "actual_paths": ["src/shared.py"],
            },
            {
                "at": "2026-08-11T00:02:20Z",
                "event": "publish-completed",
                "transaction_id": "tx-1",
                "candidate": "1234567890abcdef",
            },
            {
                "at": "2026-08-11T00:03:00Z",
                "event": "claim-released",
                "scope": "provider-b",
                "owner": "agent-b",
            },
            {
                "at": "2026-08-11T00:03:10Z",
                "event": "agent-left",
                "run_id": "run-b",
                "owner": "agent-b",
                "outcome": "completed",
                "summary": "Provider published",
            },
        ]
        for index, record in enumerate(records, start=1):
            self.write_event(index, record)

    def _seed_active_contention(self) -> None:
        payload = {
            "contention_id": "contention-1",
            "status": "open",
            "created_at": "2026-08-11T00:01:00Z",
            "coordinator": {
                "owner": "agent-b",
                "epoch": 1,
                "lease_until": "2020-01-01T00:00:00Z",
            },
            "participants": [
                {"owner": "agent-a", "scope": "route-a"},
                {"owner": "agent-b", "scope": "provider-b"},
            ],
            "responses": {"agent-b": {"accepted": True}},
            "scopes": ["route-a", "provider-b"],
            "paths": ["src/shared.py"],
            "semantic_resources": ["route:/a", "provider:b"],
            "recommendation": {
                "recommendation": "parallel-tx",
                "reason": "declared semantic writes are disjoint",
            },
        }
        (self.active / "contention-1.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_builds_semantic_storyline_from_explicit_correlations(self) -> None:
        with ObserverStore(self.data_dir) as store:
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        self.assertEqual(story["summary"]["actors"], 2)
        self.assertEqual(story["summary"]["intersections"], 1)
        self.assertEqual(story["summary"]["transactions"], 1)
        self.assertEqual(
            [lane["id"] for lane in story["lanes"][:2]],
            ["__coordination__", "__system__"],
        )
        run_a = next(
            node for node in story["nodes"] if node["id"].endswith(":run-a")
        )
        self.assertEqual(run_a["label"], "Build the route")
        contention = next(
            node for node in story["nodes"] if node["type"] == "contention"
        )
        self.assertEqual(contention["status"], "stalled")

        self.assertEqual(contention["details"]["missing_responses"], ["agent-a"])
        self.assertEqual(contention["details"]["paths"], ["src/shared.py"])

        link_types = {link["type"] for link in story["links"]}
        self.assertTrue(
            {"sequence", "handoff", "intersects", "decision", "publishes"}
            <= link_types
        )
        intersects = [
            link for link in story["links"] if link["type"] == "intersects"
        ]
        self.assertEqual(len(intersects), 2)
        self.assertTrue(
            all(link["evidence"] == "owner+scope" for link in intersects)
        )

    def test_orders_agent_lanes_by_first_visible_activity(self) -> None:
        self.write_event(
            14,
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "agent-joined",
                "run_id": "run-late",
                "owner": "agent-0",
                "task_summary": "Join after the named agents",
            },
        )
        with ObserverStore(self.data_dir) as store:
            result = store.collect()
            self.assertEqual(result["inserted"], 1)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        self.assertEqual(
            [lane["id"] for lane in story["lanes"] if lane["kind"] == "agent"],
            ["agent-a", "agent-b", "agent-0"],
        )

    def test_requires_one_project_and_bounds_visible_slices(self) -> None:
        with ObserverStore(self.data_dir) as store:
            with self.assertRaisesRegex(ValueError, "workspace_id"):
                build_collaboration_storyline(
                    store.connection,
                    since=datetime(2026, 8, 10, tzinfo=UTC),
                    workspace_id="",
                )
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
                limit=8,
            )

        self.assertEqual(story["summary"]["visible_nodes"], 8)
        self.assertTrue(story["summary"]["truncated"])
        visible = {node["id"] for node in story["nodes"]}
        self.assertTrue(
            all(
                link["source"] in visible and link["target"] in visible
                for link in story["links"]
            )
        )

    def test_active_intersection_connects_to_in_window_claim_update(self) -> None:
        self.write_event(
            14,
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "claim-updated",
                "scope": "route-a",
                "owner": "agent-a",
                "paths": ["src/shared.py"],
            },
        )
        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 1)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 11, 0, 3, 30, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        self.assertEqual(
            [node["type"] for node in story["nodes"]],
            ["contention", "claim"],
        )
        intersection = next(
            link for link in story["links"] if link["type"] == "intersects"
        )
        self.assertTrue(intersection["source"].startswith("claim:"))
        self.assertTrue(intersection["target"].startswith("contention:"))


if __name__ == "__main__":
    unittest.main()
