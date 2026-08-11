from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.project_overview import build_project_overview  # noqa: E402


class ObserverProjectOverviewTest(unittest.TestCase):
    @staticmethod
    def row(
        workspace_id: str,
        event_at: str,
        event_type: str,
        *,
        run_id: str | None = None,
        handoff_id: str | None = None,
        transaction_id: str | None = None,
        owner: str | None = None,
    ) -> dict[str, object]:
        return {
            "workspace_id": workspace_id,
            "event_at": event_at,
            "event_type": event_type,
            "run_id": run_id,
            "handoff_id": handoff_id,
            "transaction_id": transaction_id,
            "owner": owner,
            "payload_json": "{}",
        }

    def test_summarizes_projects_without_inventing_cross_project_edges(self) -> None:
        project_rows = [
            self.row("workspace-a", "2026-08-11T00:00:00Z", "agent-joined", run_id="run-a"),
            self.row("workspace-a", "2026-08-11T00:01:00Z", "message-sent", run_id="run-a"),
            self.row(
                "workspace-a",
                "2026-08-11T00:02:00Z",
                "handoff-offered",
                handoff_id="handoff-a",
            ),
            self.row(
                "workspace-a",
                "2026-08-11T00:03:00Z",
                "transaction-activated",
                transaction_id="transaction-a",
            ),
            self.row(
                "workspace-a",
                "2026-08-11T00:04:00Z",
                "refresh-conflicted",
                transaction_id="transaction-a",
            ),
            self.row("workspace-b", "2026-08-10T00:00:00Z", "agent-joined", run_id="run-b"),
            self.row("workspace-b", "2026-08-10T00:01:00Z", "agent-left", run_id="run-b"),
        ]
        result = build_project_overview(
            workspace_names={
                "workspace-a": "/workspaces/alpha",
                "workspace-b": "/workspaces/beta",
            },
            rows=project_rows,
            window_rows=project_rows[:5],
            coordination_state={
                "active_contentions": [
                    {
                        "workspace_id": "workspace-a",
                        "contention_id": "contention-a",
                        "stalled": True,
                    }
                ]
            },
        )

        self.assertEqual(result["summary"]["projects"], 2)
        self.assertEqual(result["summary"]["active_projects"], 1)
        self.assertEqual(result["summary"]["stalled_projects"], 1)
        alpha = result["projects"][0]
        self.assertEqual(alpha["workspace_id"], "workspace-a")
        self.assertEqual(alpha["events_in_window"], 5)
        self.assertEqual(alpha["active_runs"], 1)
        self.assertEqual(alpha["pending_handoffs"], 1)
        self.assertEqual(alpha["active_contentions"], 1)
        self.assertEqual(alpha["stalled_contentions"], 1)
        self.assertEqual(alpha["transactions_observed"], 1)
        self.assertEqual(alpha["transaction_conflicts"], 1)
        self.assertEqual(alpha["collaboration_signals"], 4)
        self.assertTrue(result["cross_project"]["tracking_supported"])
        self.assertFalse(result["cross_project"]["causal_tracking_supported"])
        self.assertEqual(result["cross_project"]["observed_relations"], 0)

    def test_includes_owner_identity_projection_for_global_project_view(self) -> None:
        project_rows = [
            self.row(
                "workspace-a",
                "2026-08-11T00:00:00Z",
                "agent-joined",
                run_id="run-a",
                owner="agent-shared",
            ),
            self.row(
                "workspace-b",
                "2026-08-11T00:00:10Z",
                "agent-joined",
                run_id="run-b",
                owner="agent-shared",
            ),
        ]
        result = build_project_overview(
            workspace_names={
                "workspace-a": "/workspaces/alpha",
                "workspace-b": "/workspaces/beta",
            },
            rows=project_rows,
            window_rows=project_rows,
            coordination_state={"active_contentions": []},
        )

        cross_project = result["cross_project"]
        self.assertEqual(cross_project["summary"]["owners"], 1)
        self.assertEqual(cross_project["summary"]["projects"], 2)
        self.assertEqual(cross_project["observed_relations"], 1)


if __name__ == "__main__":
    unittest.main()
