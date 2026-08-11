from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.cross_project import (  # noqa: E402
    build_cross_project_projection,
)


class ObserverCrossProjectTest(unittest.TestCase):
    @staticmethod
    def row(
        workspace_id: str,
        event_at: str,
        owner: str,
        *,
        event_type: str = "claim-created",
        run_id: str | None = None,
        task: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "at": event_at,
            "event": event_type,
            "owner": owner,
        }
        if run_id:
            payload["run_id"] = run_id
        if task:
            payload["task"] = task
        return {
            "workspace_id": workspace_id,
            "event_at": event_at,
            "event_type": event_type,
            "run_id": run_id,
            "owner": owner,
            "payload_json": json.dumps(payload),
        }

    def test_links_exact_owner_across_projects_without_causal_direction(self) -> None:
        rows = [
            self.row(
                "workspace-a",
                "2026-08-12T01:00:00Z",
                "root-infra-discovery-20260812",
                event_type="agent-joined",
                run_id="run-infer-discovery",
                task="Publish discovery contract",
            ),
            self.row(
                "workspace-a",
                "2026-08-12T01:12:00Z",
                "root-infra-discovery-20260812",
                event_type="agent-left",
                run_id="run-infer-discovery",
            ),
            self.row(
                "workspace-b",
                "2026-08-12T01:04:00Z",
                "root-infra-discovery-20260812",
                event_type="agent-joined",
                run_id="run-shape-discovery",
                task="Consume discovery contract",
            ),
            self.row(
                "workspace-b",
                "2026-08-12T01:13:00Z",
                "root-infra-discovery-20260812",
                event_type="agent-left",
                run_id="run-shape-discovery",
            ),
            self.row(
                "workspace-a",
                "2026-08-12T01:08:00Z",
                "project-local-owner",
            ),
        ]
        projection = build_cross_project_projection(
            workspace_names={
                "workspace-a": "/lab/infer-runtime",
                "workspace-b": "/lab/shape",
            },
            rows=rows,
        )

        self.assertTrue(projection["tracking_supported"])
        self.assertFalse(projection["causal_tracking_supported"])
        self.assertEqual(projection["summary"]["owners"], 1)
        self.assertEqual(projection["summary"]["projects"], 2)
        self.assertEqual(projection["summary"]["episodes"], 2)
        self.assertEqual(projection["summary"]["inferred_relations"], 1)
        relation = projection["relations"][0]
        self.assertEqual(relation["kind"], "owner-identity")
        self.assertEqual(relation["temporal_relation"], "overlap")
        self.assertEqual(relation["overlap_seconds"], 8 * 60)
        self.assertEqual(relation["confidence"], "strong")
        self.assertFalse(relation["directed"])
        self.assertEqual(
            relation["evidence"], ["owner-exact", "time-overlap"]
        )
        episodes = projection["episodes"]
        self.assertEqual(
            {tuple(episode["run_ids"]) for episode in episodes},
            {("run-infer-discovery",), ("run-shape-discovery",)},
        )
        self.assertEqual(
            {task for episode in episodes for task in episode["tasks"]},
            {"Publish discovery contract", "Consume discovery contract"},
        )

    def test_ignores_generic_owner_and_keeps_long_gap_visibly_inferred(self) -> None:
        rows = [
            self.row("workspace-a", "2026-08-12T00:00:00Z", "root"),
            self.row("workspace-b", "2026-08-12T00:01:00Z", "root"),
            self.row("workspace-a", "2026-08-12T00:00:00Z", "agent-release"),
            self.row("workspace-b", "2026-08-12T02:00:00Z", "agent-release"),
        ]
        projection = build_cross_project_projection(
            workspace_names={
                "workspace-a": "/lab/alpha",
                "workspace-b": "/lab/beta",
            },
            rows=rows,
        )

        self.assertEqual(projection["summary"]["owners"], 1)
        self.assertEqual(projection["summary"]["ignored_generic_events"], 2)
        self.assertNotIn("root", {item["owner"] for item in projection["episodes"]})
        relation = projection["relations"][0]
        self.assertEqual(relation["temporal_relation"], "sequence")
        self.assertEqual(relation["gap_seconds"], 2 * 60 * 60)
        self.assertEqual(relation["confidence"], "moderate")

    def test_requires_the_same_owner_in_more_than_one_workspace(self) -> None:
        projection = build_cross_project_projection(
            workspace_names={
                "workspace-a": "/lab/alpha",
                "workspace-b": "/lab/beta",
            },
            rows=[
                self.row("workspace-a", "2026-08-12T00:00:00Z", "agent-a"),
                self.row("workspace-b", "2026-08-12T00:00:00Z", "agent-b"),
            ],
        )

        self.assertEqual(projection["summary"]["owners"], 0)
        self.assertEqual(projection["episodes"], [])
        self.assertEqual(projection["relations"], [])


if __name__ == "__main__":
    unittest.main()
