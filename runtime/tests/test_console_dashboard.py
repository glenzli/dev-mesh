from __future__ import annotations

from pathlib import Path

from dev_mesh_coord import contention, transactions
from dev_mesh_coord.control_plane import initialize
from dev_mesh_coord.interactions import acknowledge, send
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

    def test_pending_acknowledgement_updates_after_ack(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="ack dashboard request")
        message = send(
            self.root,
            source_owner="agent-a",
            source_run_id="run-a",
            target_owner="agent-b",
            subject="review",
            body="please review",
            interaction_kind="request",
            requires_ack=True,
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            pending = build_dashboard(catalog.connection, window_hours=48)["operational"]
        self.assertEqual(pending["pending_acknowledgements"]["count"], 1)

        acknowledge(
            self.root,
            message_id=str(message["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            acknowledged = build_dashboard(catalog.connection, window_hours=48)["operational"]
        self.assertEqual(acknowledged["pending_acknowledgements"]["count"], 0)
        self.assertEqual(acknowledged["pending_acknowledgements"]["acknowledged"], 1)

    def test_contention_events_include_exact_participant_lanes(self) -> None:
        join_run(
            self.root,
            run_id="run-primary",
            owner="agent-primary",
            task="primary dashboard work",
        )
        create_claim(
            self.root,
            scope="scope-primary",
            owner="agent-primary",
            run_id="run-primary",
            task="primary dashboard work",
            paths=["app.txt"],
            intent="local-edit",
        )
        join_run(self.root, run_id="run-b", owner="agent-b", task="overlapping dashboard work")
        requested = create_claim(
            self.root,
            scope="scope-b",
            owner="agent-b",
            run_id="run-b",
            task="overlapping dashboard work",
            paths=["app.txt"],
            intent="semantic-edit",
            allow_overlap=True,
        )
        contention.propose(
            self.root,
            contention_id=str(requested["contention_id"]),
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="wait",
            reason="wait for the primary owner",
        )
        contention.respond(
            self.root,
            contention_id=str(requested["contention_id"]),
            scope="scope-primary",
            owner="agent-primary",
            run_id="run-primary",
            revision=1,
            accept=True,
            reason="accepted",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            dashboard = build_dashboard(catalog.connection, window_hours=48)
        opened = next(
            event for event in dashboard["events"] if event["event"] == "contention-opened"
        )
        self.assertEqual(
            {
                (item["owner"], item["run_id"], item["scope"])
                for item in opened["details"]["contention_participants"]
            },
            {
                ("agent-primary", "run-primary", "scope-primary"),
                ("agent-b", "run-b", "scope-b"),
            },
        )
        proposed = next(
            event
            for event in dashboard["events"]
            if event["event"] == "contention-decision-proposed"
        )
        responded = next(
            event
            for event in dashboard["events"]
            if event["event"] == "contention-decision-responded"
        )
        self.assertEqual(proposed["details"]["decision"], "wait")
        self.assertEqual(proposed["details"]["revision"], 1)
        self.assertIs(responded["details"]["accepted"], True)
        self.assertEqual(responded["details"]["revision"], 1)

    def test_transaction_events_are_enriched_with_branch_identity(self) -> None:
        create_claim(
            self.root,
            scope="scope-primary",
            owner="agent-a",
            run_id="run-a",
            task="primary branch owner",
            paths=["other.txt"],
        )
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel branch work")
        pending = create_claim(
            self.root,
            scope="scope-parallel",
            owner="agent-b",
            run_id="run-b",
            task="parallel branch work",
            paths=["other.txt"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="parallel-tx",
            reason="use one bounded temporary branch",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (
            ("scope-primary", "agent-a", "run-a"),
            ("scope-parallel", "agent-b", "run-b"),
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
        contention.enact(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
        )
        started = transactions.begin(
            self.root,
            scope="scope-parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=contention_id,
            reason="bounded dashboard microtransaction",
        )

        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            dashboard = build_dashboard(catalog.connection, window_hours=48)
        created = next(
            event
            for event in dashboard["events"]
            if event["event"] == "transaction-created"
        )
        self.assertEqual(created["transaction_id"], started["transaction_id"])
        self.assertEqual(created["details"]["branch"], started["branch"])
        self.assertEqual(created["details"]["canonical_branch"], "main")


if __name__ == "__main__":
    import unittest

    unittest.main()
