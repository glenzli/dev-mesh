from __future__ import annotations

import json
import shutil
from pathlib import Path

from dev_mesh_coord.constants import AUTHORITY_EFFECTS, PROTOCOL, PROTOCOL_VERSION, STATE_DIRECTORIES
from dev_mesh_coord.control_plane import initialize
from dev_mesh_observer.catalog import Catalog, workspace_id
from dev_mesh_observer.dashboard import build_dashboard

from helpers import GitWorkspaceTest, git


OLD_VERSION = "20260814.1"
CREATED_AT = "2026-08-14T00:00:00Z"


def install_source(root: Path, version: str, *, with_event: bool = True) -> None:
    namespace = root / ".dev-mesh"
    state = namespace / "coord" / version
    state.mkdir(parents=True)
    for relative in STATE_DIRECTORIES:
        (state / relative).mkdir(parents=True, exist_ok=True)
    records = {
        namespace / "manifest.json": {
            "schema": 1,
            "kind": "dev-mesh.workspace",
            "created_at": CREATED_AT,
            "coord_current": "coord/current.json",
        },
        namespace / "coord/current.json": {
            "schema": 1,
            "protocol": PROTOCOL,
            "version": version,
            "event_schema": 2,
            "state": version,
            "activated_at": CREATED_AT,
        },
        state / "protocol.json": {
            "schema": 1,
            "protocol": PROTOCOL,
            "version": version,
            "event_schema": 2,
            "created_at": CREATED_AT,
        },
    }
    for path, record in records.items():
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    if not with_event:
        return
    (state / "runs/old-run.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "protocol": PROTOCOL,
                "protocol_version": version,
                "run_id": "old-run",
                "owner": "agent-old",
                "status": "active",
                "joined_at": CREATED_AT,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "events/1-old-event-agent-joined.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "protocol": PROTOCOL,
                "protocol_version": version,
                "event_id": "old-event",
                "event": "agent-joined",
                "at": CREATED_AT,
                "authority_effect": AUTHORITY_EFFECTS["agent-joined"],
                "owner": "agent-old",
                "run_id": "old-run",
                "transaction_id": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class ObserverProtocolCompatibilityTest(GitWorkspaceTest):
    def _current_workspace(self) -> Path:
        current = Path(self.temporary.name) / "current-workspace"
        current.mkdir()
        git(current, "init", "-b", "main")
        git(current, "config", "user.name", "Dev Mesh Test")
        git(current, "config", "user.email", "dev-mesh@example.invalid")
        (current / "app.txt").write_text("base\n", encoding="utf-8")
        git(current, "add", "app.txt")
        git(current, "commit", "-m", "base")
        initialize(current)
        return current

    def test_collects_supported_source_versions_into_one_current_catalog(self) -> None:
        install_source(self.root, OLD_VERSION)
        current = self._current_workspace()
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_roots([Path(self.temporary.name)])
            report = catalog.report()
            dashboard = build_dashboard(catalog.connection)

        self.assertEqual(collected["workspace_count"], 2)
        self.assertEqual(collected["discovery_issues"], [])
        self.assertEqual(report["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(
            {item["source_protocol_version"] for item in report["workspaces"]},
            {OLD_VERSION, PROTOCOL_VERSION},
        )
        self.assertNotIn(
            "observer.collection-incomplete",
            {item["code"] for item in report["diagnostics"]},
        )
        self.assertEqual(
            {item["source_protocol_version"] for item in dashboard["projects"]},
            {OLD_VERSION, PROTOCOL_VERSION},
        )
        self.assertIn(workspace_id(current), {item["workspace_id"] for item in report["workspaces"]})

    def test_unknown_source_is_a_notice_not_a_collection_failure(self) -> None:
        future_version = "20990101.1"
        install_source(self.root, future_version, with_event=False)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_roots([Path(self.temporary.name)])
            report = catalog.report()
            dashboard = build_dashboard(catalog.connection)

        self.assertEqual(collected["workspace_count"], 0)
        self.assertEqual(collected["discovery_issues"][0]["code"], "protocol_migration_required")
        diagnostic = next(
            item
            for item in report["diagnostics"]
            if item["code"] == "observer.protocol-migration-required"
        )
        self.assertEqual(diagnostic["severity"], "warning")
        self.assertEqual(diagnostic["source_protocol_version"], future_version)
        self.assertNotIn(
            "observer.collection-incomplete",
            {item["code"] for item in report["diagnostics"]},
        )
        self.assertTrue(report["cutover_readiness"]["ready"])
        self.assertIsNone(dashboard["projects"][0]["collection_error"])
        self.assertIsNotNone(dashboard["projects"][0]["protocol_notice"])

    def test_source_cutover_replaces_projection_without_missing_source_findings(self) -> None:
        install_source(self.root, OLD_VERSION)
        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            catalog.collect_workspace(self.root)
            self.assertEqual(catalog.report()["event_count"], 1)
            shutil.rmtree(self.root / ".dev-mesh")
            initialize(self.root)
            collected = catalog.collect_workspace(self.root)
            report = catalog.report()

        self.assertEqual(collected["source_protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(report["event_count"], 0)
        self.assertEqual(report["integrity"]["total"], 0)
        self.assertNotIn(
            "event.source-missing", {item["code"] for item in report["diagnostics"]}
        )

    def test_existing_catalog_gains_source_and_issue_columns(self) -> None:
        database = Path(self.temporary.name) / "old-catalog.sqlite3"
        with Catalog(database) as catalog:
            catalog.connection.execute("DROP TABLE workspaces")
            catalog.connection.execute(
                """
                CREATE TABLE workspaces (
                    workspace_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    root TEXT NOT NULL,
                    last_collected_at TEXT NOT NULL,
                    last_error TEXT,
                    last_seen_scan TEXT,
                    not_observed_since TEXT,
                    PRIMARY KEY (workspace_id, protocol_version)
                )
                """
            )
            catalog.connection.commit()
        with Catalog(database) as migrated:
            columns = {
                row[1] for row in migrated.connection.execute("PRAGMA table_info(workspaces)")
            }
        self.assertTrue({"source_protocol_version", "issue_code"}.issubset(columns))
