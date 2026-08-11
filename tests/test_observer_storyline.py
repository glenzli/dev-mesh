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
                "trace_schema": 1,
                "run_id": "run-a",
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
                "trace_schema": 1,
                "run_id": "run-b",
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
                "trace_schema": 1,
                "run_id": "run-b",
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
        self.assertEqual(story["summary"]["owner_labels_in_window"], 2)
        self.assertEqual(story["summary"]["joined_runs"], 2)
        self.assertEqual(story["summary"]["max_concurrent_runs"], 2)
        self.assertEqual(story["summary"]["intersections"], 1)
        self.assertEqual(story["summary"]["transactions"], 1)
        self.assertEqual(
            [lane["id"] for lane in story["lanes"]],
            ["__canonical__", "agent-a", "agent-b", "__system__"],
        )
        run_a = next(
            span for span in story["spans"] if span["id"].endswith(":run-a")
        )
        self.assertEqual(run_a["label"], "Build the route")
        claim_a = next(
            span for span in story["spans"] if span["kind"] == "claim" and span["label"] == "route-a"
        )
        self.assertEqual(claim_a["run_id"], "run-a")
        self.assertEqual(claim_a["run_binding"], "native")
        contention = next(
            marker
            for marker in story["markers"]
            if marker["kind"] in {"contention", "decision"}
        )
        self.assertEqual(contention["status"], "stalled")

        self.assertEqual(contention["details"]["missing_responses"], ["agent-a"])
        self.assertEqual(contention["details"]["paths"], ["src/shared.py"])

        relation_kinds = {relation["kind"] for relation in story["relations"]}
        self.assertTrue({"handoff", "contention"} <= relation_kinds)
        self.assertNotIn("fork", relation_kinds)
        self.assertNotIn("rejoin", relation_kinds)
        transaction = next(
            span for span in story["spans"] if span["kind"] == "transaction"
        )
        self.assertEqual(transaction["owner"], "__system__")
        self.assertEqual(transaction["trace_quality"], "legacy-unknown")
        self.assertEqual(story["focus"]["kind"], "contention")
        self.assertEqual(story["focus"]["status"], "stalled")
        self.assertEqual(story["focus"]["owners"], ["agent-a", "agent-b"])

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
        for index in range(14, 17):
            self.write_event(
                index,
                {
                    "at": f"2026-08-11T00:04:{index - 14:02d}Z",
                    "event": "agent-joined",
                    "run_id": f"run-extra-{index}",
                    "owner": f"agent-extra-{index}",
                    "task_summary": "Bound the visible storyline",
                },
            )
        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 3)
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

        self.assertEqual(story["summary"]["visible_items"], 8)
        self.assertTrue(story["summary"]["truncated"])
        visible = {
            item["id"] for item in [*story["spans"], *story["markers"]]
        }
        self.assertTrue(
            all(
                (not relation.get("source_item") or relation["source_item"] in visible)
                and (not relation.get("target_item") or relation["target_item"] in visible)
                for relation in story["relations"]
            )
        )

    def test_active_intersection_connects_to_in_window_claim_update(self) -> None:
        self.write_event(
            14,
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "claim-updated",
                "trace_schema": 1,
                "run_id": "run-a",
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

        self.assertEqual([span["kind"] for span in story["spans"]], ["claim"])
        self.assertEqual(
            [marker["kind"] for marker in story["markers"]], ["contention"]
        )
        intersection = next(
            relation
            for relation in story["relations"]
            if relation["kind"] == "contention"
        )
        self.assertTrue(intersection["target_item"].startswith("contention:"))
        self.assertEqual(story["focus"]["relation_id"], intersection["id"])
        self.assertEqual(story["focus"]["owners"], ["agent-a", "agent-b"])

    def test_legacy_claim_is_not_retroactively_bound_to_a_later_run(self) -> None:
        self.write_event(
            14,
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "claim-created",
                "scope": "legacy-work",
                "owner": "agent-legacy",
                "paths": ["src/legacy.py"],
            },
        )
        self.write_event(
            15,
            {
                "at": "2026-08-11T00:05:00Z",
                "event": "agent-joined",
                "run_id": "run-legacy-later",
                "owner": "agent-legacy",
                "task_summary": "Continue legacy work",
            },
        )
        self.write_event(
            16,
            {
                "at": "2026-08-11T00:06:00Z",
                "event": "claim-updated",
                "trace_schema": 1,
                "run_id": "run-legacy-later",
                "scope": "legacy-work",
                "owner": "agent-legacy",
                "paths": ["src/legacy.py"],
            },
        )
        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 3)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        claim = next(
            span
            for span in story["spans"]
            if span["kind"] == "claim" and span["label"] == "legacy-work"
        )
        self.assertIsNone(claim["run_id"])
        self.assertEqual(claim["run_binding"], "mixed")
        self.assertEqual(claim["run_ids"], ["run-legacy-later"])

    def test_complete_legacy_claim_is_inferred_into_one_containing_run(self) -> None:
        records = [
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "agent-joined",
                "run_id": "run-legacy-window",
                "owner": "agent-legacy-window",
                "task_summary": "Run one bounded legacy action",
            },
            {
                "at": "2026-08-11T00:04:10Z",
                "event": "claim-created",
                "scope": "legacy-window-work",
                "owner": "agent-legacy-window",
            },
            {
                "at": "2026-08-11T00:04:20Z",
                "event": "claim-released",
                "scope": "legacy-window-work",
                "owner": "agent-legacy-window",
            },
            {
                "at": "2026-08-11T00:04:30Z",
                "event": "agent-left",
                "run_id": "run-legacy-window",
                "owner": "agent-legacy-window",
                "outcome": "completed",
            },
        ]
        for index, record in enumerate(records, start=14):
            self.write_event(index, record)

        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 4)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        claim = next(
            span
            for span in story["spans"]
            if span["kind"] == "claim" and span["label"] == "legacy-window-work"
        )
        self.assertIsNone(claim["run_id"])
        self.assertEqual(claim["run_binding"], "inferred")
        self.assertEqual(claim["inferred_run_id"], "run-legacy-window")
        self.assertEqual(
            claim["details"]["run_inference"], "unique-owner-run-window"
        )
        self.assertEqual(
            claim["details"]["run_inference_authority"], "presentation-only"
        )
        self.assertEqual(story["summary"]["inferred_run_bindings"], 1)

    def test_legacy_claim_stays_unbound_when_run_windows_overlap(self) -> None:
        records = [
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "agent-joined",
                "run_id": "run-overlap-a",
                "owner": "agent-overlap",
            },
            {
                "at": "2026-08-11T00:04:05Z",
                "event": "agent-joined",
                "run_id": "run-overlap-b",
                "owner": "agent-overlap",
            },
            {
                "at": "2026-08-11T00:04:10Z",
                "event": "claim-created",
                "scope": "ambiguous-legacy-work",
                "owner": "agent-overlap",
            },
            {
                "at": "2026-08-11T00:04:20Z",
                "event": "claim-released",
                "scope": "ambiguous-legacy-work",
                "owner": "agent-overlap",
            },
            {
                "at": "2026-08-11T00:04:30Z",
                "event": "agent-left",
                "run_id": "run-overlap-a",
                "owner": "agent-overlap",
            },
            {
                "at": "2026-08-11T00:04:31Z",
                "event": "agent-left",
                "run_id": "run-overlap-b",
                "owner": "agent-overlap",
            },
        ]
        for index, record in enumerate(records, start=14):
            self.write_event(index, record)

        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 6)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        claim = next(
            span
            for span in story["spans"]
            if span["kind"] == "claim" and span["label"] == "ambiguous-legacy-work"
        )
        self.assertEqual(claim["run_binding"], "unbound")
        self.assertNotIn("inferred_run_id", claim)
        self.assertEqual(story["summary"]["inferred_run_bindings"], 0)

    def test_open_legacy_claim_does_not_gain_a_provisional_run(self) -> None:
        self.write_event(
            14,
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "agent-joined",
                "run_id": "run-open-legacy",
                "owner": "agent-open-legacy",
            },
        )
        self.write_event(
            15,
            {
                "at": "2026-08-11T00:04:10Z",
                "event": "claim-created",
                "scope": "open-legacy-work",
                "owner": "agent-open-legacy",
            },
        )

        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], 2)
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        claim = next(
            span
            for span in story["spans"]
            if span["kind"] == "claim" and span["label"] == "open-legacy-work"
        )
        self.assertEqual(claim["run_binding"], "unbound")
        self.assertNotIn("inferred_run_id", claim)

    def test_native_trace_draws_fork_rejoin_wait_diversion_and_reassignment(
        self,
    ) -> None:
        native_events = [
            {
                "at": "2026-08-11T00:04:00Z",
                "event": "transaction-recorded",
                "trace_schema": 1,
                "transaction_id": "tx-native",
                "owner": "agent-a",
                "work_owner": "agent-a",
                "scope": "route-a",
                "branch": "agent-tx/tx-native",
                "base_revision": "base111111111111",
                "canonical_branch": "main",
            },
            {
                "at": "2026-08-11T00:04:10Z",
                "event": "transaction-activated",
                "trace_schema": 1,
                "transaction_id": "tx-native",
                "owner": "agent-a",
                "work_owner": "agent-a",
                "branch": "agent-tx/tx-native",
                "base_revision": "base111111111111",
                "canonical_branch": "main",
            },
            {
                "at": "2026-08-11T00:04:20Z",
                "event": "transaction-handed-off",
                "trace_schema": 1,
                "transaction_id": "tx-native",
                "owner": "agent-c",
                "work_owner": "agent-c",
                "source_owner": "agent-a",
                "target_owner": "agent-c",
                "branch": "agent-tx/tx-native",
                "base_revision": "base111111111111",
                "canonical_branch": "main",
            },
            {
                "at": "2026-08-11T00:04:30Z",
                "event": "transaction-resumed",
                "trace_schema": 1,
                "transaction_id": "tx-native",
                "owner": "agent-c",
                "work_owner": "agent-c",
                "branch": "agent-tx/tx-native",
                "base_revision": "base111111111111",
                "canonical_branch": "main",
            },
            {
                "at": "2026-08-11T00:04:40Z",
                "event": "publish-completed",
                "trace_schema": 1,
                "transaction_id": "tx-native",
                "owner": "agent-c",
                "work_owner": "agent-c",
                "candidate": "candidate22222222",
                "canonical_branch": "main",
            },
            {
                "at": "2026-08-11T00:04:50Z",
                "event": "work-suspended",
                "trace_schema": 1,
                "work_state_id": "wait-a",
                "owner": "agent-a",
                "work_owner": "agent-a",
                "scope": "route-a",
                "disposition": "waiting",
                "blocked_by_owners": ["agent-b"],
            },
            {
                "at": "2026-08-11T00:05:00Z",
                "event": "work-resumed",
                "trace_schema": 1,
                "work_state_id": "wait-a",
                "owner": "agent-a",
                "work_owner": "agent-a",
                "scope": "route-a",
                "disposition": "waiting",
            },
            {
                "at": "2026-08-11T00:05:10Z",
                "event": "work-suspended",
                "trace_schema": 1,
                "work_state_id": "divert-b",
                "owner": "agent-b",
                "work_owner": "agent-b",
                "scope": "provider-b",
                "disposition": "diverted",
                "alternate_scope": "docs-b",
                "blocked_by_owners": ["agent-a"],
            },
            {
                "at": "2026-08-11T00:05:20Z",
                "event": "message-sent",
                "trace_schema": 1,
                "message_id": "message-a-b",
                "owner": "agent-a",
                "target_owner": "agent-b",
                "message_type": "tx-ready",
            },
        ]
        for index, event in enumerate(native_events, start=14):
            self.write_event(index, event)
        with ObserverStore(self.data_dir) as store:
            self.assertEqual(store.collect()["inserted"], len(native_events))
            story = build_collaboration_storyline(
                store.connection,
                since=datetime(2026, 8, 10, tzinfo=UTC),
                workspace_id=self.workspace_id,
            )

        self.assertEqual(story["schema"], 2)
        agent_lanes = [
            lane["id"] for lane in story["lanes"] if lane["kind"] == "agent"
        ]
        self.assertEqual(agent_lanes, ["agent-a", "agent-b", "agent-c"])
        segments = [
            span
            for span in story["spans"]
            if span["details"].get("transaction_id") == "tx-native"
        ]
        self.assertEqual([span["owner"] for span in segments], ["agent-a", "agent-c"])
        self.assertEqual([span["status"] for span in segments], ["handed-off", "published"])
        self.assertTrue(all(span["trace_quality"] == "native" for span in segments))
        relation_kinds = {relation["kind"] for relation in story["relations"]}
        self.assertTrue(
            {"fork", "rejoin", "reassigned", "waits-for", "diverts-to", "message"}
            <= relation_kinds
        )
        self.assertEqual(story["summary"]["branch_forks"], 1)
        self.assertEqual(story["summary"]["waits"], 2)
        self.assertEqual(story["summary"]["messages"], 1)
        self.assertGreater(story["summary"]["trace_coverage"], 0)
        self.assertEqual(story["summary"]["owner_labels_in_window"], 3)
        self.assertEqual(story["focus"]["owners"], ["agent-a", "agent-b"])


if __name__ == "__main__":
    unittest.main()
