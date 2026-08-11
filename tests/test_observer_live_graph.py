from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.catalog import discover_workspaces  # noqa: E402
from dev_mesh_observer.collector import LiveCollector  # noqa: E402
from dev_mesh_observer.graph import build_collaboration_graph  # noqa: E402
from dev_mesh_observer.operations import register_discovery  # noqa: E402
from dev_mesh_observer.state_projection import (  # noqa: E402
    project_active_contentions,
)
from dev_mesh_observer.store import ObserverStore  # noqa: E402


class ObserverLiveGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.coordination = self.workspace / ".agent-coordination"
        self.events = self.coordination / "events"
        self.active = self.coordination / "contentions" / "active"
        self.events.mkdir(parents=True)
        self.active.mkdir(parents=True)
        self.data_dir = self.base / "observer-data"
        self._seed_events()
        self._write_contention()
        sources = discover_workspaces([self.workspace], max_depth=0)
        with ObserverStore(self.data_dir) as store:
            register_discovery(store, [self.workspace], sources)
            collection = store.collect()
            self.assertEqual(collection["inserted"], 5)
            self.assertEqual(collection["active_contentions"], 1)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_event(self, name: str, payload: dict[str, object]) -> None:
        (self.events / name).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _seed_events(self) -> None:
        self.write_event(
            "001-agent-a.json",
            {
                "at": "2026-08-11T00:00:00Z",
                "event": "agent-joined",
                "run_id": "run-a",
                "owner": "agent-a",
            },
        )
        self.write_event(
            "002-agent-b.json",
            {
                "at": "2026-08-11T00:01:00Z",
                "event": "agent-joined",
                "run_id": "run-b",
                "owner": "agent-b",
                "parent_agent_id": "agent-a",
            },
        )
        self.write_event(
            "003-message.json",
            {
                "at": "2026-08-11T00:02:00Z",
                "event": "message-sent",
                "run_id": "run-a",
                "owner": "agent-a",
                "scope": "agent-b",
            },
        )
        self.write_event(
            "004-handoff.json",
            {
                "at": "2026-08-11T00:03:00Z",
                "event": "handoff-offered",
                "handoff_id": "handoff-1",
                "run_id": "run-a",
                "owner": "agent-a",
                "source_owner": "agent-a",
                "target_owner": "agent-b",
            },
        )
        self.write_event(
            "005-contention.json",
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "contention-opened",
                "contention_id": "contention-stalled",
                "owner": "agent-a",
                "owners": ["agent-a", "agent-b"],
                "paths": ["src/shared.py"],
            },
        )

    def _write_contention(self) -> None:
        payload = {
            "contention_id": "contention-stalled",
            "status": "open",
            "created_at": "2026-08-11T00:04:00Z",
            "coordinator": {
                "owner": "agent-a",
                "epoch": 1,
                "lease_until": "2026-08-11T00:05:00Z",
            },
            "participants": [
                {"owner": "agent-a", "scope": "scope-a"},
                {"owner": "agent-b", "scope": "scope-b"},
            ],
            "responses": {"agent-a": {"decision": "wait"}},
            "scopes": ["scope-a", "scope-b"],
            "paths": ["src/shared.py"],
            "semantic_resources": ["runtime:shared"],
            "recommendation": {
                "recommendation": "wait",
                "reason": "overlapping direct work is already dirty",
            },
        }
        (self.active / "contention-stalled.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_projects_stalled_contention_and_causal_graph(self) -> None:
        with ObserverStore(self.data_dir) as store:
            schema_version = store.connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            self.assertEqual(schema_version, 2)
            state = project_active_contentions(
                store.connection,
                current=datetime(2026, 8, 11, 1, tzinfo=UTC),
            )
            self.assertEqual(state["summary"]["active"], 1)
            self.assertEqual(state["summary"]["stalled"], 1)
            self.assertEqual(
                state["stalled_contentions"][0]["missing_responses"],
                ["agent-b"],
            )

            graph = build_collaboration_graph(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
            )

        node_types = {node["type"] for node in graph["nodes"]}
        self.assertEqual(node_types, {"agent", "handoff", "contention"})
        stalled = next(
            node for node in graph["nodes"] if node["type"] == "contention"
        )
        self.assertEqual(stalled["status"], "stalled")
        self.assertEqual(stalled["details"]["missing_responses"], ["agent-b"])
        edge_types = {edge["type"] for edge in graph["edges"]}
        self.assertTrue(
            {"delegated", "message", "handoff-offered", "handoff-target", "contends"}
            <= edge_types
        )

    def test_reports_pending_events_before_the_next_collection(self) -> None:
        self.write_event(
            "006-agent-left.json",
            {
                "at": "2026-08-11T00:10:00Z",
                "event": "agent-left",
                "run_id": "run-b",
                "owner": "agent-b",
            },
        )
        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.status()["summary"]["pending_events"], 1)
            self.assertEqual(store.collect()["inserted"], 1)
            self.assertEqual(store.status()["summary"]["pending_events"], 0)

    def test_background_collector_ingests_new_events(self) -> None:
        self.write_event(
            "006-agent-left.json",
            {
                "at": "2026-08-11T00:10:00Z",
                "event": "agent-left",
                "run_id": "run-b",
                "owner": "agent-b",
            },
        )
        collector = LiveCollector(
            data_dir=self.data_dir,
            max_depth=0,
            interval_seconds=0.05,
        )
        collector.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with ObserverStore(self.data_dir) as store:
                    if store.status()["summary"]["pending_events"] == 0:
                        break
                time.sleep(0.02)
            else:
                self.fail("background collection did not ingest the pending event")
        finally:
            collector.stop()
        status = collector.status()
        self.assertGreaterEqual(status["cycles"], 1)
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNone(status["last_error"])

    def test_collection_continues_when_a_registered_root_disappears(self) -> None:
        missing = self.base / "missing-workspace"
        self.workspace.rename(missing)
        collector = LiveCollector(
            data_dir=self.data_dir,
            max_depth=0,
            interval_seconds=0,
        )
        result = collector.collect_now()
        self.assertEqual(
            result["discovery"]["unavailable_roots"],
            [str(self.workspace.resolve(strict=False))],
        )
        self.assertEqual(result["collection"]["unavailable"], 1)
        self.assertIsNone(collector.status()["last_error"])

    def test_malformed_active_state_is_reported_without_blocking_events(self) -> None:
        (self.active / "broken.json").write_text("{broken\n", encoding="utf-8")
        self.write_event(
            "006-agent-left.json",
            {
                "at": "2026-08-11T00:10:00Z",
                "event": "agent-left",
                "run_id": "run-b",
                "owner": "agent-b",
            },
        )
        with ObserverStore(self.data_dir) as store:
            result = store.collect()
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(result["state_issues"], 1)
            issue = store.connection.execute(
                "SELECT kind FROM collection_issues WHERE source_name=?",
                ("contentions/active/broken.json",),
            ).fetchone()
        self.assertEqual(issue["kind"], "malformed-state")


if __name__ == "__main__":
    unittest.main()
