from __future__ import annotations

from pathlib import Path

from dev_mesh_coord import contention, cross_project, transactions
from dev_mesh_coord.control_plane import initialize
from dev_mesh_coord.interactions import acknowledge, send, withdraw
from dev_mesh_coord.lifecycle import create_claim, join_run, leave_run
from dev_mesh_observer.catalog import Catalog, workspace_id
from dev_mesh_observer.dashboard import build_dashboard

from helpers import GitWorkspaceTest, git


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

    def test_scoped_timeline_keeps_window_counts_for_other_projects(self) -> None:
        other = Path(self.temporary.name) / "other-workspace"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "config", "user.name", "Dev Mesh Test")
        git(other, "config", "user.email", "dev-mesh@example.invalid")
        (other / "app.txt").write_text("base\n", encoding="utf-8")
        git(other, "add", "app.txt")
        git(other, "commit", "-m", "base")
        initialize(other)
        join_run(other, run_id="run-other", owner="agent-other", task="other dashboard")
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(other)
            scoped = build_dashboard(
                catalog.connection,
                workspace=workspace_id(self.root),
                window_hours=48,
            )

        self.assertEqual(
            {event["workspace_id"] for event in scoped["events"]},
            {workspace_id(self.root)},
        )
        counts = {
            project["workspace_id"]: project["event_count"]
            for project in scoped["projects"]
        }
        self.assertEqual(counts[workspace_id(self.root)], 2)
        self.assertEqual(counts[workspace_id(other)], 1)

    def test_project_relations_label_same_run_names_as_inferred_hint(self) -> None:
        others = []
        for name in ("other-shared-run", "third-shared-run"):
            other = Path(self.temporary.name) / name
            other.mkdir()
            git(other, "init", "-b", "main")
            git(other, "config", "user.name", "Dev Mesh Test")
            git(other, "config", "user.email", "dev-mesh@example.invalid")
            (other / "app.txt").write_text("base\n", encoding="utf-8")
            git(other, "add", "app.txt")
            git(other, "commit", "-m", "base")
            initialize(other)
            join_run(other, run_id="run-a", owner="agent-a", task="cross-project dashboard")
            others.append(other)

        with Catalog(self.database) as catalog:
            for other in others:
                catalog.collect_workspace(other)
            dashboard = build_dashboard(
                catalog.connection,
                workspace=workspace_id(self.root),
                window_hours=48,
            )

        projection = dashboard["project_collaboration"]
        self.assertEqual(projection["project_count"], 3)
        self.assertEqual(projection["relation_count"], 1)
        self.assertEqual(projection["collaboration_relation_count"], 0)
        self.assertEqual(projection["inferred_relation_count"], 1)
        self.assertEqual(projection["edges"], [])
        group = projection["hint_groups"][0]
        self.assertEqual(
            set(group["workspace_ids"]),
            {workspace_id(self.root), *(workspace_id(other) for other in others)},
        )
        self.assertEqual(group["same_run_hint_count"], 1)
        self.assertEqual(group["samples"][0]["owner"], "agent-a")
        self.assertEqual(group["samples"][0]["run_id"], "run-a")

    def test_local_acknowledgement_does_not_upgrade_same_run_hint(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="receive project request")
        message = send(
            self.root,
            source_owner="agent-a",
            source_run_id="run-a",
            target_owner="agent-b",
            subject="cross-project review",
            body="review the related project work",
            interaction_kind="request",
            requires_ack=True,
        )
        acknowledge(
            self.root,
            message_id=str(message["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
        )
        other = Path(self.temporary.name) / "other-recipient"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "config", "user.name", "Dev Mesh Test")
        git(other, "config", "user.email", "dev-mesh@example.invalid")
        (other / "app.txt").write_text("base\n", encoding="utf-8")
        git(other, "add", "app.txt")
        git(other, "commit", "-m", "base")
        initialize(other)
        join_run(other, run_id="run-b", owner="agent-b", task="related project work")

        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            catalog.collect_workspace(other)
            dashboard = build_dashboard(catalog.connection, window_hours=48)

        projection = dashboard["project_collaboration"]
        self.assertEqual(projection["collaboration_relation_count"], 0)
        self.assertEqual(projection["inferred_relation_count"], 1)
        self.assertEqual(projection["edges"], [])
        self.assertEqual(projection["hint_groups"][0]["same_run_hint_count"], 1)

    def test_project_collaboration_joins_explicit_extension_across_distinct_runs(self) -> None:
        other = Path(self.temporary.name) / "other-explicit-collaboration"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "config", "user.name", "Dev Mesh Test")
        git(other, "config", "user.email", "dev-mesh@example.invalid")
        (other / "app.txt").write_text("base\n", encoding="utf-8")
        git(other, "add", "app.txt")
        git(other, "commit", "-m", "base")
        initialize(other)
        join_run(other, run_id="target-run", owner="target-agent", task="cross-project target")
        relation_id = "dashboard-cross-project"
        cross_project.open_collaboration(
            self.root,
            collaboration_id=relation_id,
            source_owner="agent-a",
            source_run_id="run-a",
            target_task_id="target-task",
            target_workspace_id=workspace_id(other),
            kind="dependency",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            catalog.collect_workspace(other)
            opened_dashboard = build_dashboard(catalog.connection, window_hours=48)

        self.assertEqual(
            opened_dashboard["project_collaboration"]["collaboration_relation_count"],
            0,
        )
        self.assertEqual(opened_dashboard["project_collaboration"]["relation_count"], 0)

        cross_project.bind_collaboration(
            other,
            collaboration_id=relation_id,
            source_workspace_id=workspace_id(self.root),
            source_owner="agent-a",
            source_run_id="run-a",
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="target-task",
            kind="dependency",
        )
        cross_project.close_collaboration(
            other,
            collaboration_id=relation_id,
            actor_role="target",
            owner="target-agent",
            run_id="target-run",
            source_workspace_id=workspace_id(self.root),
            source_owner="agent-a",
            source_run_id="run-a",
            target_workspace_id=workspace_id(other),
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="target-task",
            kind="dependency",
            outcome="completed",
        )

        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            catalog.collect_workspace(other)
            dashboard = build_dashboard(catalog.connection, window_hours=48)

        edge = dashboard["project_collaboration"]["edges"][0]
        projection = dashboard["project_collaboration"]
        self.assertEqual(projection["collaboration_relation_count"], 1)
        self.assertEqual(projection["inferred_relation_count"], 0)
        self.assertEqual(edge["collaboration_count"], 1)
        self.assertEqual(edge["open_collaboration_count"], 0)
        self.assertEqual(edge["completed_collaboration_count"], 1)
        self.assertEqual(
            edge["directions"],
            [
                {
                    "source_workspace_id": workspace_id(self.root),
                    "target_workspace_id": workspace_id(other),
                    "count": 1,
                }
            ],
        )
        self.assertEqual(edge["samples"][0]["collaboration_id"], relation_id)

    def test_project_collaboration_marks_terminal_bound_run_as_pending_settlement(self) -> None:
        other = Path(self.temporary.name) / "other-late-close"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "config", "user.name", "Dev Mesh Test")
        git(other, "config", "user.email", "dev-mesh@example.invalid")
        (other / "app.txt").write_text("base\n", encoding="utf-8")
        git(other, "add", "app.txt")
        git(other, "commit", "-m", "base")
        initialize(other)
        join_run(other, run_id="target-run", owner="target-agent", task="cross-project target")
        relation_id = "dashboard-late-close"
        cross_project.open_collaboration(
            self.root,
            collaboration_id=relation_id,
            source_owner="agent-a",
            source_run_id="run-a",
            target_task_id="target-task",
            target_workspace_id=workspace_id(other),
            target_owner="target-agent",
            kind="integration",
        )
        cross_project.bind_collaboration(
            other,
            collaboration_id=relation_id,
            source_workspace_id=workspace_id(self.root),
            source_owner="agent-a",
            source_run_id="run-a",
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="target-task",
            kind="integration",
        )
        leave_run(
            other,
            run_id="target-run",
            owner="target-agent",
            outcome="completed",
            summary="completed before closing relation",
        )

        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            catalog.collect_workspace(other)
            pending_dashboard = build_dashboard(catalog.connection, window_hours=48)
        pending_edge = pending_dashboard["project_collaboration"]["edges"][0]
        self.assertEqual(pending_edge["open_collaboration_count"], 1)
        self.assertEqual(pending_edge["active_collaboration_count"], 0)
        self.assertEqual(pending_edge["pending_settlement_count"], 1)
        self.assertEqual(pending_edge["samples"][0]["status"], "pending-settlement")

        join_run(
            other,
            run_id="target-close-run",
            owner="target-agent",
            task="reconcile late close",
        )
        cross_project.reconcile_closed_collaboration(
            other,
            collaboration_id=relation_id,
            owner="target-agent",
            run_id="target-close-run",
            outcome="completed",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(other)
            closed_dashboard = build_dashboard(catalog.connection, window_hours=48)
        closed_edge = closed_dashboard["project_collaboration"]["edges"][0]
        self.assertEqual(closed_edge["open_collaboration_count"], 0)
        self.assertEqual(closed_edge["pending_settlement_count"], 0)
        self.assertEqual(closed_edge["completed_collaboration_count"], 1)
        self.assertEqual(closed_edge["samples"][0]["status"], "completed")

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
            pending_dashboard = build_dashboard(catalog.connection, window_hours=48)
        self.assertEqual(
            pending_dashboard["operational"]["pending_acknowledgements"]["count"],
            1,
        )
        pending_message = next(
            event
            for event in pending_dashboard["events"]
            if event["event"] == "message-sent"
        )
        self.assertEqual(pending_message["details"]["source_run_id"], "run-a")
        self.assertNotIn("target_run_id", pending_message["details"])

        acknowledge(
            self.root,
            message_id=str(message["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            acknowledged_dashboard = build_dashboard(catalog.connection, window_hours=48)
        acknowledged = acknowledged_dashboard["operational"]
        self.assertEqual(acknowledged["pending_acknowledgements"]["count"], 0)
        self.assertEqual(acknowledged["pending_acknowledgements"]["acknowledged"], 1)
        message_events = [
            event
            for event in acknowledged_dashboard["events"]
            if event["event"] in {"message-sent", "message-acknowledged"}
        ]
        self.assertEqual(len(message_events), 2)
        for event in message_events:
            self.assertEqual(event["details"]["source_owner"], "agent-a")
            self.assertEqual(event["details"]["source_run_id"], "run-a")
            self.assertEqual(event["details"]["target_owner"], "agent-b")
            self.assertEqual(event["details"]["target_run_id"], "run-b")

    def test_terminal_handoff_and_closed_source_run_are_not_current_confirmations(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="terminal messages")
        handoff = send(
            self.root,
            source_owner="agent-b",
            source_run_id="run-b",
            target_owner="agent-a",
            subject="handoff",
            body="temporary handoff",
            interaction_kind="handoff",
            topic="takeover",
            requires_ack=True,
            handoff_id="dashboard-withdrawn-handoff",
        )
        withdraw(
            self.root,
            handoff_id="dashboard-withdrawn-handoff",
            source_owner="agent-b",
            source_run_id="run-b",
            reason_code="no-longer-needed",
            reason="request completed locally",
        )
        request = send(
            self.root,
            source_owner="agent-b",
            source_run_id="run-b",
            target_owner="agent-a",
            subject="review",
            body="historical review request",
            interaction_kind="request",
            requires_ack=True,
        )
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="completed",
            summary="terminal message projection complete",
        )

        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            dashboard = build_dashboard(catalog.connection, window_hours=48)

        confirmations = dashboard["operational"]["pending_acknowledgements"]
        self.assertEqual(confirmations["count"], 0)
        self.assertEqual(confirmations["lifecycle_resolved"], 1)
        self.assertEqual(confirmations["historical"], 1)
        self.assertEqual(
            confirmations["lifecycle_resolved_items"][0]["message_id"],
            handoff["message_id"],
        )
        self.assertEqual(
            confirmations["historical_items"][0]["message_id"],
            request["message_id"],
        )
        self.assertNotIn(
            "message.ack-stale",
            dashboard["operational"]["diagnostic_summary"]["counts"],
        )

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
            decision="exclusive",
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
        self.assertEqual(proposed["details"]["decision"], "exclusive")
        self.assertEqual(proposed["details"]["revision"], 1)
        self.assertIs(responded["details"]["accepted"], True)
        self.assertEqual(responded["details"]["revision"], 1)

    def test_accepted_handoff_projects_exact_run_pair_onto_offer(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="accept handoff")
        offered = send(
            self.root,
            source_owner="agent-a",
            source_run_id="run-a",
            target_owner="agent-b",
            subject="take over",
            body="continue the bounded task",
            interaction_kind="handoff",
            requires_ack=True,
            handoff_id="dashboard-handoff",
        )
        acknowledge(
            self.root,
            message_id=str(offered["message_id"]),
            target_owner="agent-b",
            target_run_id="run-b",
        )
        with Catalog(self.database) as catalog:
            catalog.collect_workspace(self.root)
            dashboard = build_dashboard(catalog.connection, window_hours=48)

        offer_event = next(
            event for event in dashboard["events"] if event["event"] == "handoff-offered"
        )
        self.assertEqual(
            {
                field: offer_event["details"][field]
                for field in (
                    "source_owner",
                    "source_run_id",
                    "target_owner",
                    "target_run_id",
                )
            },
            {
                "source_owner": "agent-a",
                "source_run_id": "run-a",
                "target_owner": "agent-b",
                "target_run_id": "run-b",
            },
        )

    def test_transaction_events_are_enriched_with_branch_identity(self) -> None:
        create_claim(
            self.root,
            scope="scope-primary",
            owner="agent-a",
            run_id="run-a",
            task="primary branch owner",
            paths=["other.txt"],
            semantic_writes=["primary-dashboard-slice"],
        )
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel branch work")
        pending = create_claim(
            self.root,
            scope="scope-parallel",
            owner="agent-b",
            run_id="run-b",
            task="parallel branch work",
            paths=["other.txt"],
            semantic_writes=["parallel-dashboard-slice"],
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
