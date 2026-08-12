from __future__ import annotations

from pathlib import Path

from dev_mesh_coord.control_plane import initialize
from dev_mesh_coord.lifecycle import create_claim, join_run
from dev_mesh_observer.catalog import Catalog, workspace_id
from dev_mesh_observer.dashboard import build_dashboard

from helpers import GitWorkspaceTest


class ConsoleDashboardTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="dashboard test")
        create_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            task="dashboard test",
            paths=["app.txt"],
            intent="read",
        )
        self.database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)

    def test_dashboard_projects_events_and_active_authority(self) -> None:
        identifier = workspace_id(self.root)
        with Catalog(self.database) as catalog:
            dashboard = build_dashboard(catalog.connection, window_hours=48)

        self.assertEqual(dashboard["kind"], "dev-mesh.console.dashboard")
        self.assertEqual(dashboard["selection"]["workspace_id"], None)
        self.assertEqual(len(dashboard["projects"]), 1)
        project = dashboard["projects"][0]
        self.assertEqual(project["workspace_id"], identifier)
        self.assertEqual(project["name"], self.root.name)
        self.assertEqual(project["active"], {"claim": 1, "run": 1})
        self.assertEqual(project["event_count"], 2)
        self.assertEqual(
            [item["event"] for item in dashboard["events"]],
            ["agent-joined", "claim-created"],
        )
        self.assertEqual(
            {(item["kind"], item["object_id"]) for item in dashboard["active_details"]},
            {("run", "run-a"), ("claim", "scope-a")},
        )
        self.assertEqual(dashboard["operational"]["active"], {"run": 1, "claim": 1})

    def test_workspace_filter_is_exact_and_bounds_are_enforced(self) -> None:
        identifier = workspace_id(self.root)
        with Catalog(self.database) as catalog:
            scoped = build_dashboard(
                catalog.connection,
                workspace=identifier,
                window_hours=6,
                event_limit=1,
            )
            with self.assertRaisesRegex(ValueError, "unknown workspace"):
                build_dashboard(catalog.connection, workspace="missing")
            with self.assertRaisesRegex(ValueError, "unsupported observation window"):
                build_dashboard(catalog.connection, window_hours=2)
            with self.assertRaisesRegex(ValueError, "event limit"):
                build_dashboard(catalog.connection, event_limit=401)

        self.assertEqual(scoped["selection"]["workspace_id"], identifier)
        self.assertTrue(scoped["selection"]["events_truncated"])
        self.assertEqual(len(scoped["events"]), 1)
        self.assertEqual({item["workspace_id"] for item in scoped["events"]}, {identifier})


if __name__ == "__main__":
    import unittest

    unittest.main()
