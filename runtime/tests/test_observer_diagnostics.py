from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dev_mesh_coord import canonical_git, contention, work, work_results
from dev_mesh_coord.constants import (
    AUTHORITY_EFFECTS,
    EVENT_SCHEMA,
    MAX_EVENT_BYTES,
    PROTOCOL,
    PROTOCOL_VERSION,
)
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    activate_pending_claim,
    create_claim,
    join_run,
    leave_run,
    release_claim,
)
from dev_mesh_observer.catalog import Catalog, workspace_id
from dev_mesh_observer.reports import ACTIVE_STATUSES
from dev_mesh_observer.source_validation import MAX_SNAPSHOT_BYTES, SNAPSHOT_STATUSES

from helpers import GitWorkspaceTest, git


OLD = "2026-08-12T00:00:00.000000Z"
FUTURE_EXPIRED = "2026-08-12T00:01:00.000000Z"


class ObserverIntegrityTest(GitWorkspaceTest):
    def test_contention_participants_are_not_reported_as_non_collaborative(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-active", owner="agent-active", task="first edit")
        join_run(self.root, run_id="run-pending", owner="agent-pending", task="later edit")
        create_claim(
            self.root,
            scope="active-scope",
            owner="agent-active",
            run_id="run-active",
            task="first edit",
            paths=["app.txt"],
        )
        pending = create_claim(
            self.root,
            scope="pending-scope",
            owner="agent-pending",
            run_id="run-pending",
            task="later edit",
            paths=["app.txt"],
        )
        contention.select_wait(
            self.root,
            contention_id=str(pending["contention_id"]),
            scope="pending-scope",
            owner="agent-pending",
            run_id="run-pending",
            reason="first edit is short",
        )
        release_claim(
            self.root,
            scope="active-scope",
            owner="agent-active",
            run_id="run-active",
            summary="first edit done",
        )
        leave_run(
            self.root,
            run_id="run-active",
            owner="agent-active",
            outcome="completed",
            summary="first edit done",
        )
        activate_pending_claim(
            self.root,
            scope="pending-scope",
            owner="agent-pending",
            run_id="run-pending",
        )
        release_claim(
            self.root,
            scope="pending-scope",
            owner="agent-pending",
            run_id="run-pending",
            summary="later edit done",
        )
        leave_run(
            self.root,
            run_id="run-pending",
            owner="agent-pending",
            outcome="completed",
            summary="later edit done",
        )

        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))

        self.assertEqual(report["non_collaborative_runs"], [])
        self.assertIn(
            {
                "source": "agent-pending",
                "target": "agent-active",
                "event": "contention",
                "count": 1,
            },
            report["owner_edges"],
        )

    def test_completed_dirty_work_is_non_authoritative_and_cutover_ready(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="record result")
        create_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            task="record result",
            paths=["app.txt"],
        )
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        work_results.complete_claim(
            self.root,
            result_id="result-a",
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            summary="dirty work completed",
            validation_evidence="focused validation passed",
        )
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="result recorded",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        self.assertEqual(
            report["work_results"],
            {
                "recorded": 1,
                "projection_modes": {"git-tree": 1},
                "completed_events": 1,
                "awaiting_baseline_acknowledgement": 0,
                "completion_pending": 0,
            },
        )
        self.assertNotIn("claim", report["active"])
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])

    def test_workspace_bytes_result_is_reported_without_git_publication(self) -> None:
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "ignore local data")
        initialize(self.root)
        join_run(self.root, run_id="run-data", owner="agent-data", task="write local data")
        create_claim(
            self.root,
            scope="local-data",
            owner="agent-data",
            run_id="run-data",
            task="write ignored local data",
            paths=["local/state.json"],
            projection_mode="workspace-bytes",
        )
        (self.root / "local").mkdir()
        (self.root / "local/state.json").write_text('{"ready":true}\n', encoding="utf-8")
        work_results.complete_claim(
            self.root,
            result_id="local-data-result",
            scope="local-data",
            owner="agent-data",
            run_id="run-data",
            summary="wrote ignored local data",
            validation_evidence="JSON decoded successfully",
        )
        leave_run(
            self.root,
            run_id="run-data",
            owner="agent-data",
            outcome="completed",
            summary="local data recorded",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(report["work_results"]["projection_modes"], {"workspace-bytes": 1})
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])

    def test_real_direct_commit_producer_converges_in_observer(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="direct commit")
        create_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            task="direct commit",
            paths=["app.txt"],
            intent="local-edit",
        )
        (self.root / "app.txt").write_text("direct commit\n", encoding="utf-8")
        direct = canonical_git.commit(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            summary="direct commit",
            validation_evidence="focused validation passed",
        )
        self.assertEqual(direct["status"], "completed")
        release_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            summary="direct commit released",
        )
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="direct commit complete",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        self.assertEqual(report["direct_commit"], {
            "started": 1,
            "completed": 1,
            "active": 0,
            "outcomes": {"completed": 1, "needs_attention": 0},
        })
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])

    def test_normal_work_resume_archive_collects_without_path_false_positive(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="work archive")
        create_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            task="work archive",
            paths=["app.txt"],
        )
        disposition = work.suspend(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            disposition="waiting",
            reason="bounded dependency",
        )
        work.resume(
            self.root,
            work_state_id=str(disposition["work_state_id"]),
            owner="agent-a",
            run_id="run-a",
            evidence="dependency completed",
        )
        release_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            summary="work archive complete",
        )
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="work archive complete",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        self.assertEqual(report["lifecycle_status_counts"]["work:archive:resumed"], 1)
        self.assertEqual(report["diagnostic_summary"]["total"], 0)
        self.assertTrue(report["cutover_readiness"]["ready"])

    def test_oversized_known_event_is_persistent_mutation_evidence(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="mutation size")
        event_path = next((resolve(self.root).state_root / "events").glob("*.json"))
        original = event_path.read_bytes()
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            changed = json.loads(original.decode("utf-8"))
            changed["blob"] = "x" * MAX_EVENT_BYTES
            event_path.write_text(json.dumps(changed) + "\n", encoding="utf-8")
            catalog.collect_workspace(self.root)
            event_path.write_bytes(original)
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(report["integrity"]["counts"]["event.source-mutated"], 1)
        self.assertNotIn("event.source-invalid", report["integrity"]["counts"])
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_open_grant_events_without_authority_snapshots_block_readiness(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="audit gap")
        create_claim(
            self.root,
            scope="scope-a",
            owner="agent-a",
            run_id="run-a",
            task="audit gap",
            paths=["app.txt"],
        )
        plane = resolve(self.root)
        (plane.state_root / "runs/run-a.json").unlink()
        (plane.state_root / "claims/scope-a.json").unlink()
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("run.open-event-without-snapshot", codes)
        self.assertIn("claim.open-event-without-snapshot", codes)
        self.assertEqual(report["cutover_readiness"]["blockers"]["audit_gaps"], 2)
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_integrity_aggregates_and_invalid_samples_are_exact_and_bounded(self) -> None:
        initialize(self.root)
        event_directory = resolve(self.root).state_root / "events"
        invalid_paths = []
        for index in range(300):
            path = event_directory / f"invalid-{index:03d}.json"
            path.write_text("{}\n", encoding="utf-8")
            invalid_paths.append(path)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            first = catalog.collect_workspace(self.root)
            blocked = catalog.report(workspace=workspace_id(self.root))
            for path in invalid_paths:
                path.unlink()
            second = catalog.collect_workspace(self.root)
            clean = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(first["invalid_count"], 300)
        self.assertEqual(len(first["invalid_records"]), 20)
        self.assertTrue(first["invalid_records_truncated"])
        self.assertEqual(blocked["integrity"]["counts"]["event.source-invalid"], 300)
        self.assertEqual(blocked["diagnostic_summary"]["counts"]["event.source-invalid"], 300)
        self.assertEqual(blocked["cutover_readiness"]["blockers"]["integrity_findings"], 300)
        self.assertEqual(second["invalid_count"], 0)
        self.assertEqual(clean["integrity"]["total"], 0)
        self.assertEqual(clean["integrity"]["historical_total"], 300)
        self.assertEqual(clean["integrity"]["resolved_total"], 300)
        self.assertTrue(clean["cutover_readiness"]["ready"])

    def test_structural_and_size_violations_are_collection_errors(self) -> None:
        initialize(self.root)
        plane = resolve(self.root)
        malformed_run = {
            "schema": 1,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "status": "closed",
        }
        (plane.state_root / "runs/broken.json").write_text(
            json.dumps(malformed_run) + "\n", encoding="utf-8"
        )
        oversized_snapshot = {
            "schema": 1,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "run_id": "oversized-run",
            "owner": "agent-a",
            "status": "closed",
            "joined_at": OLD,
            "blob": "x" * MAX_SNAPSHOT_BYTES,
        }
        oversized_snapshot_path = plane.state_root / "runs/oversized-run.json"
        oversized_snapshot_path.write_text(
            json.dumps(oversized_snapshot) + "\n", encoding="utf-8"
        )
        self.assertGreater(oversized_snapshot_path.stat().st_size, MAX_SNAPSHOT_BYTES)
        malformed_event = {
            "schema": EVENT_SCHEMA,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "event_id": "bad-time",
            "event": "agent-left",
            "at": "not-a-time",
            "authority_effect": AUTHORITY_EFFECTS["agent-left"],
            "owner": "agent-a",
            "run_id": "run-a",
            "transaction_id": None,
        }
        (plane.state_root / "events/1-bad-time-agent-left.json").write_text(
            json.dumps(malformed_event) + "\n", encoding="utf-8"
        )
        oversized = {
            "schema": EVENT_SCHEMA,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "event_id": "oversized",
            "event": "audit-correction",
            "at": OLD,
            "authority_effect": AUTHORITY_EFFECTS["audit-correction"],
            "owner": "agent-a",
            "run_id": "run-a",
            "transaction_id": None,
            "scope": "scope-a",
            "supersedes_event_id": "older",
            "blob": "x" * MAX_EVENT_BYTES,
        }
        oversized_path = plane.state_root / "events/2-oversized-audit-correction.json"
        oversized_path.write_text(json.dumps(oversized) + "\n", encoding="utf-8")
        self.assertGreater(oversized_path.stat().st_size, MAX_EVENT_BYTES)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 4)
        self.assertEqual(report["event_count"], 0)
        self.assertEqual(report["snapshots"], {})
        self.assertEqual(report["integrity"]["counts"]["event.source-invalid"], 2)
        self.assertEqual(report["cutover_readiness"]["blockers"]["collection_errors"], 1)

    def test_known_workspace_missing_from_latest_scan_is_not_ready(self) -> None:
        initialize(self.root)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            with patch("dev_mesh_observer.catalog.now", return_value=OLD):
                first = catalog.collect_roots([Path(self.temporary.name)])
                marker = self.root / ".dev-mesh/manifest.json"
                marker.rename(self.root / ".dev-mesh/manifest.hidden")
                second = catalog.collect_roots([Path(self.temporary.name)])
                report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(first["workspace_count"], 1)
        self.assertEqual(second["workspace_count"], 0)
        self.assertEqual(first["scan_at"], second["scan_at"])
        self.assertNotEqual(first["scan_id"], second["scan_id"])
        self.assertIn(
            "observer.workspace-not-observed",
            {item["code"] for item in report["diagnostics"]},
        )
        self.assertEqual(
            report["cutover_readiness"]["blockers"]["workspaces_not_observed"], 1
        )

    def test_source_event_mutation_is_persisted_without_replacing_original(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="original task")
        event_path = next((resolve(self.root).state_root / "events").glob("*.json"))
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            original = catalog.connection.execute(
                "SELECT record_json FROM events WHERE event = 'agent-joined'"
            ).fetchone()[0]

            changed = json.loads(event_path.read_text(encoding="utf-8"))
            changed["task"] = "tampered after collection"
            changed["event_id"] = "replacement-event-id"
            event_path.write_text(json.dumps(changed, sort_keys=True) + "\n", encoding="utf-8")
            second = catalog.collect_workspace(self.root)
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
            retained = catalog.connection.execute(
                "SELECT record_json FROM events WHERE event = 'agent-joined'"
            ).fetchone()[0]

        self.assertIn("immutable event source changed", "\n".join(second["invalid_records"]))
        self.assertEqual(json.loads(retained), json.loads(original))
        self.assertEqual(report["integrity"]["source_mutations"], 1)
        self.assertEqual(
            [item["code"] for item in report["diagnostics"]].count("event.source-mutated"),
            1,
        )
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_missing_and_first_seen_invalid_event_sources_block_readiness(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="source presence")
        plane = resolve(self.root)
        joined_event = next(
            path
            for path in plane.state_root.joinpath("events").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("event") == "agent-joined"
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            leave_run(
                self.root,
                run_id="run-a",
                owner="agent-a",
                outcome="completed",
                summary="source presence complete",
            )
            joined_event.unlink()
            (plane.state_root / "events/invalid.json").write_text("{}\n", encoding="utf-8")
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        counts = report["integrity"]["counts"]
        self.assertEqual(counts["event.source-missing"], 1)
        self.assertEqual(counts["event.source-invalid"], 1)
        self.assertTrue(collected["invalid_records"])
        self.assertFalse(report["cutover_readiness"]["ready"])
        self.assertIn("collection_errors", report["cutover_readiness"]["blockers"])

    def test_invalid_active_snapshot_makes_collection_incomplete_and_not_ready(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="invalid snapshot")
        run_path = resolve(self.root).state_root / "runs/run-a.json"
        run_path.write_text("{}\n", encoding="utf-8")
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertTrue(collected["invalid_records"])
        self.assertFalse(report["cutover_readiness"]["ready"])
        self.assertEqual(report["cutover_readiness"]["blockers"]["collection_errors"], 1)

    def test_unpublished_catalog_rebuilds_only_replaceable_snapshot_projection(self) -> None:
        database = Path(self.temporary.name) / "old-observer.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE events (
                    workspace_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    authority_effect TEXT NOT NULL,
                    owner TEXT,
                    run_id TEXT,
                    scope TEXT,
                    transaction_id TEXT,
                    contention_id TEXT,
                    handoff_id TEXT,
                    record_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, protocol_version, event_id)
                );
                CREATE TABLE snapshots (
                    workspace_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    status TEXT,
                    record_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, protocol_version, kind, object_id)
                );
                INSERT INTO snapshots VALUES (
                    'workspace', '20260812.1', 'contention', 'old', 'completed', '{}', 'old'
                );
                """
            )
        with Catalog(database) as catalog:
            event_columns = {
                row[1] for row in catalog.connection.execute("PRAGMA table_info(events)")
            }
            snapshot_columns = {
                row[1] for row in catalog.connection.execute("PRAGMA table_info(snapshots)")
            }
            snapshots = catalog.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertIn("source_sha256", event_columns)
        self.assertTrue({"lifecycle", "source_path"}.issubset(snapshot_columns))
        self.assertEqual(snapshots, 0)

    def test_reused_claim_scope_keeps_distinct_archived_instances(self) -> None:
        initialize(self.root)
        for suffix in ("one", "two"):
            run_id = f"run-{suffix}"
            join_run(self.root, run_id=run_id, owner="agent-a", task=f"edit {suffix}")
            create_claim(
                self.root,
                scope="reused-scope",
                owner="agent-a",
                run_id=run_id,
                task=f"edit {suffix}",
                paths=["app.txt"],
            )
            release_claim(
                self.root,
                scope="reused-scope",
                owner="agent-a",
                run_id=run_id,
                summary=f"edit {suffix} done",
            )
            leave_run(
                self.root,
                run_id=run_id,
                owner="agent-a",
                outcome="completed",
                summary=f"edit {suffix} done",
            )

        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
            archived = catalog.connection.execute(
                """
                SELECT source_path FROM snapshots
                WHERE kind = 'claim' AND object_id = 'reused-scope' AND lifecycle = 'archive'
                ORDER BY source_path
                """
            ).fetchall()

        self.assertEqual(collected["invalid_records"], [])
        self.assertEqual(len(archived), 2)
        self.assertNotEqual(archived[0]["source_path"], archived[1]["source_path"])
        self.assertNotIn(
            "claim.multiple-terminal-events",
            {item["code"] for item in report["diagnostics"]},
        )
        self.assertTrue(report["cutover_readiness"]["ready"])


class ObserverDiagnosticProjectionTest(GitWorkspaceTest):
    def _snapshot(self, relative: str, value: dict[str, object]) -> None:
        plane = resolve(self.root)
        path = plane.state_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        kind = next(
            name
            for prefix, name in (
                ("runs/", "run"),
                ("claims/", "claim"),
                ("handoffs/", "handoff"),
                ("contentions/", "contention"),
                ("transactions/", "transaction"),
                ("direct-commits/", "direct-commit"),
                ("cleanups/", "cleanup"),
                ("work/", "work"),
            )
            if relative.startswith(prefix)
        )
        defaults: dict[str, object] = {
            "run": {},
            "claim": {"created_at": OLD, "heartbeat_at": OLD},
            "handoff": {"offered_at": OLD},
            "contention": {"opened_at": OLD},
            "transaction": {"owner": "agent-live", "run_id": "run-live", "created_at": OLD},
            "direct-commit": {
                "owner": "agent-live",
                "run_id": "run-live",
                "scope": "direct-scope",
                "canonical_branch": "main",
                "base_revision": "base-revision",
                "created_at": OLD,
            },
            "cleanup": {"owner": "agent-live", "run_id": "run-live", "created_at": OLD},
            "work": {"scope": "work-scope", "suspended_at": OLD},
        }[kind]
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "protocol": PROTOCOL,
                    "protocol_version": PROTOCOL_VERSION,
                    **defaults,
                    **value,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _event(
        self,
        event_id: str,
        event: str,
        *,
        owner: str | None = None,
        run_id: str | None = None,
        **payload: object,
    ) -> None:
        plane = resolve(self.root)
        record = {
            "schema": EVENT_SCHEMA,
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "event_id": event_id,
            "event": event,
            "at": OLD,
            "authority_effect": AUTHORITY_EFFECTS[event],
            "owner": owner or "agent-live",
            "run_id": run_id or "run-live",
            "transaction_id": None,
            **payload,
        }
        (plane.state_root / "events" / f"1-{event_id}-{event}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def test_archives_and_live_state_expose_v1_failure_classes(self) -> None:
        initialize(self.root)
        self._snapshot(
            "runs/run-live.json",
            {"run_id": "run-live", "owner": "agent-live", "status": "active", "joined_at": OLD},
        )
        self._snapshot(
            "runs/run-dead.json",
            {
                "run_id": "run-dead",
                "owner": "agent-dead",
                "status": "closed",
                "outcome": "abandoned",
                "joined_at": OLD,
            },
        )
        self._snapshot(
            "runs/run-solo.json",
            {
                "run_id": "run-solo",
                "owner": "agent-solo",
                "status": "closed",
                "outcome": "completed",
                "joined_at": OLD,
            },
        )
        self._event("joined-live", "agent-joined", owner="agent-live", run_id="run-live")
        self._event("joined-solo", "agent-joined", owner="agent-solo", run_id="run-solo")
        self._event("left-solo", "agent-left", owner="agent-solo", run_id="run-solo")

        self._snapshot(
            "contentions/active/contention-live.json",
            {
                "contention_id": "contention-live",
                "status": "awaiting-acks",
                "scopes": ["live"],
                "participants": [
                    {"scope": "live", "owner": "agent-live", "run_id": "run-live"}
                ],
                "coordinator": {
                    "owner": "agent-live",
                    "run_id": "run-live",
                    "epoch": 1,
                    "lease_expires_at": FUTURE_EXPIRED,
                },
            },
        )
        self._snapshot(
            "contentions/active/contention-orphan.json",
            {
                "contention_id": "contention-orphan",
                "status": "awaiting-decision",
                "scopes": ["orphan"],
                "participants": [
                    {"scope": "orphan", "owner": "agent-dead", "run_id": "run-dead"}
                ],
                "coordinator": {
                    "owner": "agent-dead",
                    "run_id": "run-dead",
                    "epoch": 1,
                    "lease_expires_at": FUTURE_EXPIRED,
                },
            },
        )
        self._snapshot(
            "contentions/archive/contention-gap.json",
            {"contention_id": "contention-gap", "status": "completed"},
        )
        self._snapshot(
            "handoffs/handoff-orphan.json",
            {
                "handoff_id": "handoff-orphan",
                "source_owner": "agent-dead",
                "source_run_id": "run-dead",
                "target_owner": "agent-live",
                "status": "offered",
            },
        )
        self._snapshot(
            "handoffs/handoff-active-terminal.json",
            {
                "handoff_id": "handoff-active-terminal",
                "source_owner": "agent-live",
                "source_run_id": "run-live",
                "target_owner": "agent-dead",
                "status": "offered",
            },
        )
        self._event(
            "accepted-active-handoff",
            "handoff-accepted",
            owner="agent-dead",
            run_id="run-dead",
            handoff_id="handoff-active-terminal",
        )
        self._snapshot(
            "transactions/active/tx-active-terminal.json",
            {
                "transaction_id": "tx-active-terminal",
                "owner": "agent-live",
                "run_id": "run-live",
                "status": "aborting",
            },
        )
        self._event(
            "aborted-active",
            "transaction-aborted",
            transaction_id="tx-active-terminal",
        )
        self._snapshot(
            "transactions/archive/tx-ok.json",
            {
                "transaction_id": "tx-ok",
                "owner": "agent-dead",
                "run_id": "run-dead",
                "status": "aborted",
            },
        )
        self._event(
            "aborted-ok",
            "transaction-aborted",
            owner="agent-dead",
            run_id="run-dead",
            transaction_id="tx-ok",
        )
        self._snapshot(
            "cleanups/archive/cleanup-gap.json",
            {"cleanup_id": "cleanup-gap", "transaction_id": "cleanup-gap", "status": "completed"},
        )
        self._snapshot(
            "cleanups/active/cleanup-attention.json",
            {
                "cleanup_id": "cleanup-attention",
                "transaction_id": "cleanup-attention",
                "status": "needs-attention",
            },
        )
        self._snapshot(
            "work/archive/1-work-gap.json",
            {
                "work_state_id": "work-gap",
                "owner": "agent-dead",
                "run_id": "run-dead",
                "status": "resumed",
            },
        )

        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(
                workspace=workspace_id(self.root), stale_after_seconds=1
            )

        codes = {item["code"] for item in report["diagnostics"]}
        self.assertEqual(collected["snapshots"], 13, collected["invalid_records"])
        self.assertIn("contention.live-stalled", codes)
        self.assertIn("contention.orphaned", codes)
        self.assertIn("contention.terminal-event-missing", codes)
        self.assertIn("handoff.source-terminal", codes)
        self.assertIn("handoff.terminal-event-with-active-snapshot", codes)
        self.assertIn("transaction.terminal-event-with-active-snapshot", codes)
        self.assertIn("cleanup.terminal-event-missing", codes)
        self.assertIn("cleanup.needs-attention", codes)
        self.assertIn("work.terminal-event-missing", codes)
        self.assertIn("run.unclosed", codes)
        self.assertNotIn("run.open-without-claim", codes)
        self.assertNotIn("run.stale", codes)
        self.assertEqual(report["active"]["transaction"], 1)
        self.assertEqual(report["active"]["cleanup"], 1)
        self.assertEqual(
            report["lifecycle_status_counts"]["contention:archive:completed"], 1
        )
        self.assertFalse(report["cutover_readiness"]["ready"])
        self.assertEqual(report["cutover_readiness"]["blockers"]["active_contentions"], 2)
        self.assertNotIn(
            "run-live", {item["run_id"] for item in report["non_collaborative_runs"]}
        )
        self.assertIn(
            "run-solo", {item["run_id"] for item in report["non_collaborative_runs"]}
        )

    def test_empty_initialized_workspace_is_cutover_ready(self) -> None:
        initialize(self.root)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertTrue(report["cutover_readiness"]["ready"])
        self.assertEqual(report["cutover_readiness"]["blockers"], {})

    def test_pending_acknowledgements_are_projected_and_stale_requests_are_diagnosed(self) -> None:
        initialize(self.root)
        self._snapshot(
            "runs/run-live.json",
            {"run_id": "run-live", "owner": "agent-live", "status": "active", "joined_at": OLD},
        )
        self._snapshot(
            "runs/run-closed.json",
            {
                "run_id": "run-closed",
                "owner": "agent-closed",
                "status": "closed",
                "outcome": "completed",
                "joined_at": OLD,
            },
        )
        self._snapshot(
            "handoffs/handoff-withdrawn.json",
            {
                "handoff_id": "handoff-withdrawn",
                "message_id": "message-handoff",
                "source_owner": "agent-live",
                "source_run_id": "run-live",
                "target_owner": "agent-target",
                "status": "withdrawn",
            },
        )
        self._event(
            "joined-closed",
            "agent-joined",
            owner="agent-closed",
            run_id="run-closed",
        )
        self._event(
            "left-closed",
            "agent-left",
            owner="agent-closed",
            run_id="run-closed",
        )
        self._event(
            "handoff-offered",
            "handoff-offered",
            handoff_id="handoff-withdrawn",
            message_id="message-handoff",
        )
        self._event(
            "handoff-withdrawn",
            "handoff-withdrawn",
            handoff_id="handoff-withdrawn",
            status="withdrawn",
        )
        self._event(
            "message-pending",
            "message-sent",
            message_id="message-pending",
            source_owner="agent-live",
            target_owner="agent-target",
            requires_ack=True,
            topic="coordination",
        )
        self._event(
            "message-acked",
            "message-sent",
            message_id="message-acked",
            source_owner="agent-live",
            target_owner="agent-target",
            requires_ack=True,
            topic="coordination",
        )
        self._event(
            "message-ack",
            "message-acknowledged",
            owner="agent-target",
            run_id="run-target",
            message_id="message-acked",
            interaction_kind="request",
        )
        self._event(
            "message-historical",
            "message-sent",
            owner="agent-closed",
            run_id="run-closed",
            message_id="message-historical",
            source_owner="agent-closed",
            target_owner="agent-target",
            requires_ack=True,
            topic="coordination",
        )
        self._event(
            "message-handoff",
            "message-sent",
            message_id="message-handoff",
            source_owner="agent-live",
            target_owner="agent-target",
            requires_ack=True,
            topic="takeover",
            handoff_id="handoff-withdrawn",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root), stale_after_seconds=1)
        pending = report["pending_acknowledgements"]
        self.assertEqual(
            {
                key: pending[key]
                for key in (
                    "count",
                    "requested",
                    "acknowledged",
                    "lifecycle_resolved",
                    "historical",
                )
            },
            {
                "count": 1,
                "requested": 4,
                "acknowledged": 1,
                "lifecycle_resolved": 1,
                "historical": 1,
            },
        )
        self.assertEqual(pending["items"][0]["message_id"], "message-pending")
        self.assertEqual(
            pending["lifecycle_resolved_items"][0]["message_id"],
            "message-handoff",
        )
        self.assertEqual(
            pending["historical_items"][0]["message_id"],
            "message-historical",
        )
        self.assertIn("message.ack-stale", report["diagnostic_summary"]["counts"])
        stale = next(
            item
            for item in report["diagnostics"]
            if item["code"] == "message.ack-stale"
        )
        self.assertEqual(
            {
                key: stale[key]
                for key in ("source_owner", "target_owner", "topic")
            },
            {
                "source_owner": "agent-live",
                "target_owner": "agent-target",
                "topic": "coordination",
            },
        )
        self.assertIsInstance(stale["at"], str)
        self.assertNotIn("audit_gaps", report["cutover_readiness"]["blockers"])

    def test_claim_heartbeat_warns_before_it_times_out(self) -> None:
        initialize(self.root)
        heartbeat = (datetime.now(UTC) - timedelta(seconds=85)).isoformat().replace(
            "+00:00", "Z"
        )
        self._snapshot(
            "runs/run-aging.json",
            {
                "run_id": "run-aging",
                "owner": "agent-aging",
                "status": "active",
                "joined_at": heartbeat,
            },
        )
        self._snapshot(
            "claims/scope-aging.json",
            {
                "scope": "scope-aging",
                "owner": "agent-aging",
                "run_id": "run-aging",
                "status": "active",
                "heartbeat_at": heartbeat,
            },
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root), stale_after_seconds=100)
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("claim.heartbeat-aging", codes)
        self.assertNotIn("claim.heartbeat-stale", codes)
        self.assertNotIn("run.stale", codes)

    def test_persisted_recovery_and_terminal_statuses_remain_visible(self) -> None:
        expected_active = {
            "claim": {"released", "published", "aborted"},
            "contention": {"finalizing", "completed", "cancelled"},
            "transaction": {
                "refreshing",
                "refresh-needs-attention",
                "publishing",
                "published",
                "aborting",
                "aborted",
            },
            "direct-commit": {"staging", "committing", "needs-attention", "completed"},
            "cleanup": {
                "removing-worktree",
                "removing-branch",
                "archive-pending",
                "completed",
            },
            "work": {"finalizing", "resumed"},
        }
        for kind, statuses in expected_active.items():
            lifecycle = "current" if kind == "claim" else "active"
            self.assertTrue(statuses.issubset(SNAPSHOT_STATUSES[(kind, lifecycle)]))
            self.assertTrue(statuses.issubset(ACTIVE_STATUSES[kind]))

        initialize(self.root)
        self._snapshot(
            "runs/run-live.json",
            {"run_id": "run-live", "owner": "agent-live", "status": "active", "joined_at": OLD},
        )
        self._event("joined-live", "agent-joined", owner="agent-live", run_id="run-live")
        self._snapshot(
            "claims/claim-terminal.json",
            {
                "scope": "claim-terminal",
                "owner": "agent-live",
                "run_id": "run-live",
                "status": "released",
            },
        )
        self._event(
            "claim-created-terminal",
            "claim-created",
            owner="agent-live",
            run_id="run-live",
            scope="claim-terminal",
        )
        self._event(
            "claim-released-terminal",
            "claim-released",
            owner="agent-live",
            run_id="run-live",
            scope="claim-terminal",
        )
        self._snapshot(
            "contentions/active/contention-terminal.json",
            {
                "contention_id": "contention-terminal",
                "status": "completed",
                "participants": [
                    {"scope": "claim-terminal", "owner": "agent-live", "run_id": "run-live"}
                ],
                "coordinator": {
                    "owner": "agent-live",
                    "run_id": "run-live",
                    "epoch": 1,
                    "lease_expires_at": FUTURE_EXPIRED,
                },
            },
        )
        self._event(
            "contention-completed-terminal",
            "contention-completed",
            owner="agent-live",
            run_id="run-live",
            contention_id="contention-terminal",
        )
        self._snapshot(
            "transactions/active/tx-terminal.json",
            {"transaction_id": "tx-terminal", "status": "aborted"},
        )
        self._event(
            "transaction-aborted-terminal",
            "transaction-aborted",
            owner="agent-live",
            run_id="run-live",
            transaction_id="tx-terminal",
        )
        self._snapshot(
            "cleanups/active/cleanup-terminal.json",
            {
                "cleanup_id": "cleanup-terminal",
                "transaction_id": "tx-terminal",
                "status": "completed",
            },
        )
        self._event(
            "cleanup-completed-terminal",
            "cleanup-completed",
            owner="agent-live",
            run_id="run-live",
            cleanup_id="cleanup-terminal",
            transaction_id="tx-terminal",
        )
        self._snapshot(
            "work/active/work-terminal.json",
            {
                "work_state_id": "work-terminal",
                "scope": "claim-terminal",
                "owner": "agent-live",
                "run_id": "run-live",
                "status": "resumed",
            },
        )
        self._event(
            "work-resumed-terminal",
            "work-resumed",
            owner="agent-live",
            run_id="run-live",
            work_state_id="work-terminal",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("claim.finalization-pending", codes)
        for kind in ("contention", "transaction", "cleanup", "work"):
            self.assertIn(f"{kind}.terminal-event-with-active-snapshot", codes)
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_duplicate_and_conflicting_terminal_events_are_audit_gaps(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="terminal audit")
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="terminal audit complete",
        )
        plane = resolve(self.root)
        left_path = next(
            path
            for path in (plane.state_root / "events").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("event") == "agent-left"
        )
        duplicate = json.loads(left_path.read_text(encoding="utf-8"))
        duplicate["event_id"] = "duplicate-left"
        (plane.state_root / "events/1-duplicate-left-agent-left.json").write_text(
            json.dumps(duplicate) + "\n", encoding="utf-8"
        )
        self._snapshot(
            "handoffs/handoff-a.json",
            {
                "handoff_id": "handoff-a",
                "source_owner": "agent-a",
                "source_run_id": "run-a",
                "target_owner": "agent-b",
                "status": "accepted",
            },
        )
        self._event(
            "handoff-accepted-one",
            "handoff-accepted",
            owner="agent-a",
            run_id="run-a",
            handoff_id="handoff-a",
        )
        self._event(
            "handoff-rejected-one",
            "handoff-rejected",
            owner="agent-a",
            run_id="run-a",
            handoff_id="handoff-a",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("run.multiple-terminal-events", codes)
        self.assertIn("handoff.conflicting-terminal-events", codes)
        self.assertEqual(report["cutover_readiness"]["blockers"]["audit_gaps"], 2)
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_direct_commit_lifecycle_is_distinct_bounded_and_audited(self) -> None:
        initialize(self.root)
        self._snapshot(
            "runs/run-live.json",
            {"run_id": "run-live", "owner": "agent-live", "status": "active", "joined_at": OLD},
        )
        self._event("joined-live", "agent-joined", owner="agent-live", run_id="run-live")
        self._snapshot(
            "direct-commits/active/direct-commit-attention.json",
            {
                "direct_commit_id": "direct-commit-attention",
                "scope": "direct-scope",
                "status": "needs-attention",
            },
        )
        self._event(
            "direct-started-attention",
            "direct-commit-started",
            owner="agent-live",
            run_id="run-live",
            direct_commit_id="direct-commit-attention",
            scope="direct-scope",
        )
        self._snapshot(
            "direct-commits/archive/direct-commit-complete.json",
            {
                "direct_commit_id": "direct-commit-complete",
                "scope": "direct-scope",
                "status": "completed",
            },
        )
        self._event(
            "direct-started-complete",
            "direct-commit-started",
            owner="agent-live",
            run_id="run-live",
            direct_commit_id="direct-commit-complete",
            scope="direct-scope",
        )
        self._event(
            "direct-completed-one",
            "direct-commit-completed",
            owner="agent-live",
            run_id="run-live",
            direct_commit_id="direct-commit-complete",
            scope="direct-scope",
        )
        self._event(
            "direct-completed-two",
            "direct-commit-completed",
            owner="agent-live",
            run_id="run-live",
            direct_commit_id="direct-commit-complete",
            scope="direct-scope",
        )
        self._event(
            "direct-started-missing",
            "direct-commit-started",
            owner="agent-live",
            run_id="run-live",
            direct_commit_id="direct-commit-missing",
            scope="direct-scope",
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        self.assertEqual(report["direct_commit"]["started"], 3)
        self.assertEqual(report["direct_commit"]["completed"], 2)
        self.assertEqual(report["direct_commit"]["active"], 1)
        self.assertEqual(report["transaction_outcomes"], {
            "published": 0,
            "aborted": 0,
            "conflicted": 0,
            "refreshed": 0,
        })
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertIn("direct-commit.needs-attention", codes)
        self.assertIn("direct-commit.multiple-terminal-events", codes)
        self.assertIn("direct-commit.open-event-without-snapshot", codes)
        self.assertEqual(report["cutover_readiness"]["blockers"]["active_direct_commits"], 1)
        self.assertFalse(report["cutover_readiness"]["ready"])

    def test_archived_preflight_rejection_is_retained_without_breaking_collection(self) -> None:
        initialize(self.root)
        self._snapshot(
            "direct-commits/archive/direct-commit-preflight-aborted-before-index-write.json",
            {
                "direct_commit_id": "direct-commit-preflight",
                "scope": "direct-scope",
                "status": "needs-attention",
                "error": "git index write was denied before a commit intent existed",
            },
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertEqual(collected["invalid_count"], 0)
        self.assertEqual(report["status_counts"]["direct-commit:needs-attention"], 1)
        self.assertEqual(report["direct_commit"]["active"], 0)
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertNotIn("direct-commit.archive-nonterminal", codes)

    def test_finalizing_contention_is_active_visible_and_archived_wait_is_not_orphaned(self) -> None:
        initialize(self.root)
        self._snapshot(
            "runs/run-live.json",
            {"run_id": "run-live", "owner": "agent-live", "status": "active", "joined_at": OLD},
        )
        self._snapshot(
            "claims/pending.json",
            {
                "scope": "pending",
                "owner": "agent-live",
                "run_id": "run-live",
                "status": "pending-arbitration",
                "contention_id": "contention-archived",
            },
        )
        self._snapshot(
            "contentions/archive/contention-archived.json",
            {
                "contention_id": "contention-archived",
                "status": "completed",
                "decision": "wait",
            },
        )
        self._event(
            "completed-archived",
            "contention-completed",
            owner="agent-live",
            run_id="run-live",
            contention_id="contention-archived",
        )
        self._snapshot(
            "contentions/active/contention-finalizing.json",
            {
                "contention_id": "contention-finalizing",
                "status": "finalizing",
                "scopes": ["pending"],
                "participants": [
                    {"scope": "pending", "owner": "agent-live", "run_id": "run-live"}
                ],
                "coordinator": {
                    "owner": "agent-live",
                    "run_id": "run-live",
                    "epoch": 1,
                    "lease_expires_at": FUTURE_EXPIRED,
                },
            },
        )
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            report = catalog.report(workspace=workspace_id(self.root), stale_after_seconds=1)
        codes = {item["code"] for item in report["diagnostics"]}
        self.assertEqual(report["active"]["contention"], 1)
        self.assertIn("contention.finalization-pending", codes)
        self.assertNotIn("claim.pending-without-contention", codes)
        self.assertFalse(report["cutover_readiness"]["ready"])
        self.assertEqual(report["cutover_readiness"]["blockers"]["active_contentions"], 1)
