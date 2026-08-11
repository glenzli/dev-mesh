from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = (
    PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
)
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.catalog import discover_workspaces  # noqa: E402
from dev_mesh_observer.console import (  # noqa: E402
    CONTENT_SECURITY_POLICY,
    ObserverConsole,
    validate_loopback_host,
)
from dev_mesh_observer.operations import register_discovery  # noqa: E402
from dev_mesh_observer.store import ObserverStore  # noqa: E402


class ObserverConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.events = self.workspace / ".agent-coordination" / "events"
        self.events.mkdir(parents=True)
        self.data_dir = self.base / "observer-data"
        self.write_event(
            "001-agent-joined.json",
            {
                "at": "2026-08-11T00:00:00Z",
                "event": "agent-joined",
                "run_id": "run-console",
                "owner": "agent-console",
                "scope": "console-test",
            },
        )
        sources = discover_workspaces([self.workspace], max_depth=0)
        with ObserverStore(self.data_dir) as store:
            register_discovery(store, [self.workspace], sources)
            result = store.collect()
            self.assertEqual(result["inserted"], 1)

        self.server = ObserverConsole(
            "127.0.0.1",
            0,
            data_dir=self.data_dir,
            max_depth=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def write_event(self, name: str, payload: dict[str, object]) -> None:
        (self.events / name).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def source_snapshot(self) -> dict[str, str]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.events.glob("*.json"))
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        response_headers = {name: value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, response_headers, payload

    def json_request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        status, _, payload = self.request(
            method,
            path,
            body=body,
            headers=headers,
        )
        return status, json.loads(payload.decode("utf-8"))

    def test_serves_console_assets_with_browser_security_headers(self) -> None:
        status, headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], CONTENT_SECURITY_POLICY)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("geolocation=()", headers["Permissions-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn(b"Dev Mesh Observer", payload)
        self.assertIn(b"storyline-tooltip", payload)
        self.assertNotIn(b"/storyline-demo.js", payload)
        self.assertIn(b"storyline.legend.inferred", payload)
        self.assertIn(b"storyline.legend.unbound", payload)
        self.assertIn(b"storyline.legend.handoff", payload)
        self.assertIn(b"storyline.legend.message", payload)
        self.assertIn(b"storyline.legend.diverted", payload)
        self.assertIn(b"storyline.legend.contention", payload)
        self.assertIn(b'class="compact-control window-control"', payload)
        self.assertIn(b'id="since-select"', payload)
        self.assertIn(b'id="scope-workspace"', payload)
        self.assertIn(b'id="event-pagination"', payload)
        self.assertIn(b'id="event-page-size"', payload)
        self.assertNotIn(b'class="hero"', payload)
        self.assertNotIn(b'id="page-title"', payload)
        self.assertNotIn(b"storyline.mode.entities", payload)
        self.assertNotIn(b"/graph-view.js", payload)
        self.assertNotIn(b'id="graph-layout"', payload)
        self.assertNotIn(b'id="graph-workspace"', payload)

        status, _, script = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"X-Dev-Mesh-Console", script)
        self.assertIn(b"/api/v1/workspaces", script)
        self.assertIn(b"collector.liveWithDelta", script)
        self.assertIn(b"collector.collectingWithDelta", script)
        self.assertIn(b"resetTimelineSnapshot", script)
        self.assertIn(b"payload.pagination", script)
        self.assertIn(b"workspaceScope", script)
        self.assertIn(b"syncLocation", script)
        self.assertNotIn(b"DEMO_WORKSPACE_ID", script)
        self.assertNotIn(b"DevMeshStorylineDemo", script)
        self.assertNotIn(b"/api/v1/graph", script)
        status, _, analytics = self.request("GET", "/analytics-view.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshAnalyticsView", analytics)
        status, _, project_overview = self.request("GET", "/project-overview.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshProjectOverview", project_overview)
        status, _, storyline = self.request("GET", "/storyline-view.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshStorylineView", storyline)
        self.assertIn(b"showTooltip", storyline)
        self.assertIn(b"drawProgress", storyline)
        self.assertIn(b"progress-arrow", storyline)
        self.assertIn(b"run_binding", storyline)
        self.assertIn(b"storyline.lane.actor", storyline)
        self.assertIn(b"latestRecordedAt", storyline)
        self.assertIn(b"branch-arrow", storyline)
        self.assertIn(b"publish-arrow", storyline)
        self.assertIn(b"communication-arrow", storyline)
        self.assertIn(b"waiting-arrow", storyline)
        self.assertIn(b"diverted-arrow", storyline)
        self.assertIn(b"relationArrow", storyline)
        self.assertIn(b'spine.final ? "url(#progress-arrow)" : undefined', storyline)
        self.assertIn(b"axisAligned", storyline)
        self.assertIn(b"middleX", storyline)
        self.assertIn(b" H ${middleX} V ", storyline)
        self.assertIn(b"branchTransitionPath", storyline)
        self.assertIn(b'kind === "fork"', storyline)
        self.assertIn(b"canonicalBranchPath", storyline)
        self.assertIn(b"drawCanonicalBranches", storyline)
        self.assertIn(b"local-fork-node", storyline)
        self.assertIn(b"local-rejoin-node", storyline)
        self.assertNotIn(b"trace-branch-glyph", storyline)
        self.assertNotIn(b"OWNER_COLORS", storyline)
        self.assertNotIn(b"relation-label", storyline)
        status, _, storyline_focus = self.request("GET", "/storyline-focus.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshStorylineFocus", storyline_focus)
        self.assertIn(b"relationOwners", storyline_focus)
        status, _, storyline_layout = self.request("GET", "/storyline-layout.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshStorylineLayout", storyline_layout)
        self.assertIn(b"MOMENT_STEP", storyline_layout)
        self.assertIn(b"runSpineGeometry", storyline_layout)
        self.assertIn(b"attachmentGeometry", storyline_layout)
        self.assertIn(b"compoundGroups", storyline_layout)
        self.assertIn(b"NODE_TIER", storyline_layout)
        self.assertIn(b"BRANCH_TIER", storyline_layout)
        self.assertIn(b"transactionContextById", storyline_layout)
        self.assertIn(b"branchTransactionId", storyline_layout)
        self.assertIn(b"visibleRunSpineGeometry", storyline_layout)
        self.assertIn(b"coveredIntervalsBySpine", storyline_layout)
        self.assertIn(b"branchEpisodes", storyline_layout)
        self.assertIn(b"canonicalBranchGeometry", storyline_layout)
        self.assertIn(b"CANONICAL_BRANCH_STEP", storyline_layout)
        self.assertIn(b"momentGeometry", storyline_layout)
        self.assertIn(b'["waiting", "diverted"].includes(span.kind)', storyline_layout)
        self.assertNotIn(b"branchIntervalsBySpine", storyline_layout)
        status, _, graph = self.request("GET", "/graph-view.js")
        self.assertEqual(status, 200)
        self.assertIn(b"DevMeshGraphView", graph)
        status, _, preferences = self.request("GET", "/preferences.js")
        self.assertEqual(status, 200)
        self.assertIn(b"localStorage", preferences)
        self.assertIn(b"DevMeshPreferences", preferences)
        self.assertIn(b"storyline.runBinding.inferred", preferences)
        self.assertIn(b"storyline.runBinding.unbound", preferences)
        self.assertIn(b"storyline.countWithLatest", preferences)
        self.assertIn(b"storyline.countWithProjectLatest", preferences)
        self.assertNotIn(b"storyline.demo.selector", preferences)
        self.assertNotIn(b"storyline.quality.synthetic", preferences)
        status, _, storyline_demo = self.request("GET", "/storyline-demo.js")
        self.assertEqual(status, 404)
        self.assertEqual(storyline_demo, b'{"error":"not found"}')
        status, _, stylesheet = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--lime", stylesheet)
        self.assertIn(b"prefers-color-scheme", stylesheet)
        self.assertIn(b'data-theme="dark"', stylesheet)
        self.assertIn(b"--trace-main", stylesheet)
        self.assertIn(b"--trace-branch", stylesheet)
        self.assertIn(b".communication-arrow", stylesheet)
        self.assertIn(b".waiting-arrow", stylesheet)
        self.assertIn(b".diverted-arrow", stylesheet)
        self.assertIn(b".canonical-branch-path", stylesheet)
        self.assertIn(b".canonical-fork-node", stylesheet)
        self.assertIn(b".canonical-rejoin-node", stylesheet)

    def test_reads_status_reports_events_and_details(self) -> None:
        before = self.source_snapshot()
        status, catalog = self.json_request("GET", "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["summary"]["workspaces"], 1)
        self.assertEqual(catalog["summary"]["events"], 1)
        self.assertEqual(catalog["summary"]["pending_events"], 0)
        self.assertFalse(catalog["collector"]["enabled"])

        status, report = self.json_request("GET", "/api/v1/report?since=7d")
        self.assertEqual(status, 200)
        self.assertEqual(report["scope"]["kind"], "all")
        self.assertEqual(report["summary"]["runs_open"], 1)
        self.assertIn("coordination_analytics", report)
        self.assertIn("coordination_state", report)
        self.assertIn("project_overview", report)
        self.assertEqual(report["project_overview"]["summary"]["projects"], 1)
        self.assertFalse(
            report["project_overview"]["cross_project"]["tracking_supported"]
        )
        self.assertEqual(
            report["coordination_analytics"]["protocol_use"]["summary"][
                "open_unclassified"
            ],
            1,
        )

        status, graph = self.json_request("GET", "/api/v1/graph?since=7d")
        self.assertEqual(status, 200)
        self.assertEqual(graph["summary"]["visible_nodes"], 1)
        self.assertEqual(graph["nodes"][0]["type"], "agent")

        workspace_id = str(catalog["workspaces"][0]["workspace_id"])
        status, scoped_report = self.json_request(
            "GET",
            f"/api/v1/report?since=7d&workspace_id={workspace_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(scoped_report["scope"]["kind"], "workspace")
        self.assertEqual(scoped_report["scope"]["workspace_id"], workspace_id)
        self.assertEqual(scoped_report["summary"]["registered_workspaces"], 1)
        self.assertEqual(scoped_report["summary"]["events_in_window"], 1)
        self.assertEqual(scoped_report["project_overview"]["summary"]["projects"], 1)

        status, scoped_issues = self.json_request(
            "GET",
            f"/api/v1/issues?workspace_id={workspace_id}&limit=100",
        )
        self.assertEqual(status, 200)
        self.assertEqual(scoped_issues["scope"]["workspace_id"], workspace_id)
        self.assertEqual(scoped_issues["total"], 0)
        self.assertEqual(scoped_issues["issues"], [])

        status, storyline = self.json_request(
            "GET",
            f"/api/v1/storyline?since=7d&workspace_id={workspace_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(storyline["schema"], 2)
        self.assertEqual(storyline["summary"]["visible_items"], 1)
        self.assertEqual(storyline["spans"][0]["kind"], "session")
        self.assertEqual(storyline["lanes"][0]["kind"], "canonical")
        self.assertEqual(storyline["workspace_id"], workspace_id)

        status, result = self.json_request(
            "GET",
            "/api/v1/events?owner=agent-console&limit=10",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["pagination"]["page"], 1)
        self.assertEqual(result["pagination"]["page_size"], 10)
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["pagination"]["pages"], 1)
        self.assertEqual(result["pagination"]["newer"], 0)
        event = result["events"][0]
        self.assertNotIn("payload", event)

        detail_path = (
            "/api/v1/event?workspace_id="
            f"{event['workspace_id']}&source_name={event['source_name']}"
        )
        status, detail = self.json_request("GET", detail_path)
        self.assertEqual(status, 200)
        self.assertEqual(detail["payload"]["run_id"], "run-console")
        self.assertEqual(self.source_snapshot(), before)

    def test_collect_requires_console_header_and_uses_registered_roots(self) -> None:
        status, rejected = self.json_request(
            "POST",
            "/api/v1/collect",
            body="{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("header", rejected["error"])

        self.write_event(
            "002-agent-left.json",
            {
                "at": "2026-08-11T00:05:00Z",
                "event": "agent-left",
                "run_id": "run-console",
                "owner": "agent-console",
            },
        )
        before = self.source_snapshot()
        status, collected = self.json_request(
            "POST",
            "/api/v1/collect",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "X-Dev-Mesh-Console": "1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(collected["collection"]["inserted"], 1)
        self.assertEqual(self.source_snapshot(), before)

    def test_event_pages_remain_stable_while_new_events_are_collected(self) -> None:
        self.write_event(
            "002-agent-left.json",
            {
                "at": "2026-08-11T00:05:00Z",
                "event": "agent-left",
                "run_id": "run-console",
                "owner": "agent-console",
            },
        )
        status, collected = self.json_request(
            "POST",
            "/api/v1/collect",
            body="{}",
            headers={"Content-Type": "application/json", "X-Dev-Mesh-Console": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(collected["collection"]["inserted"], 1)

        status, first_page = self.json_request(
            "GET",
            "/api/v1/events?owner=agent-console&limit=1&page=1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(first_page["events"][0]["event_type"], "agent-left")
        self.assertEqual(first_page["pagination"]["total"], 2)
        self.assertEqual(first_page["pagination"]["pages"], 2)
        anchor = first_page["pagination"]["anchor"]

        self.write_event(
            "003-agent-joined.json",
            {
                "at": "2026-08-11T00:10:00Z",
                "event": "agent-joined",
                "run_id": "run-later",
                "owner": "agent-console",
            },
        )
        status, collected = self.json_request(
            "POST",
            "/api/v1/collect",
            body="{}",
            headers={"Content-Type": "application/json", "X-Dev-Mesh-Console": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(collected["collection"]["inserted"], 1)

        status, second_page = self.json_request(
            "GET",
            f"/api/v1/events?owner=agent-console&limit=1&page=2&anchor={anchor}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(second_page["events"][0]["event_type"], "agent-joined")
        self.assertEqual(second_page["events"][0]["run_id"], "run-console")
        self.assertEqual(second_page["pagination"]["total"], 2)
        self.assertEqual(second_page["pagination"]["newer"], 1)

        status, latest_page = self.json_request(
            "GET",
            "/api/v1/events?owner=agent-console&limit=1&page=1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(latest_page["pagination"]["total"], 3)
        self.assertEqual(latest_page["events"][0]["run_id"], "run-later")

    def test_adds_an_explicit_workspace_root_and_collects_without_source_writes(self) -> None:
        added_workspace = self.base / "added-workspace"
        added_events = added_workspace / ".agent-coordination" / "events"
        added_events.mkdir(parents=True)
        added_event = added_events / "001-agent-joined.json"
        added_event.write_text(
            json.dumps(
                {
                    "at": "2026-08-11T00:10:00Z",
                    "event": "agent-joined",
                    "run_id": "run-added",
                    "owner": "agent-added",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (added_events / "002-malformed.json").write_text("{\n", encoding="utf-8")
        before = hashlib.sha256(added_event.read_bytes()).hexdigest()
        body = json.dumps({"root": str(added_workspace), "max_depth": 0})
        status, result = self.json_request(
            "POST",
            "/api/v1/workspaces",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Dev-Mesh-Console": "1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["discovery"]["registered"], 1)
        self.assertEqual(result["collection"]["inserted"], 1)
        self.assertEqual(hashlib.sha256(added_event.read_bytes()).hexdigest(), before)

        status, catalog = self.json_request("GET", "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["summary"]["workspaces"], 2)
        self.assertIn(str(added_workspace.resolve()), catalog["scan_roots"])
        added_record = next(
            workspace
            for workspace in catalog["workspaces"]
            if workspace["workspace_root"] == str(added_workspace.resolve())
        )
        original_record = next(
            workspace
            for workspace in catalog["workspaces"]
            if workspace["workspace_root"] == str(self.workspace.resolve())
        )
        added_id = str(added_record["workspace_id"])
        original_id = str(original_record["workspace_id"])

        status, scoped_report = self.json_request(
            "GET",
            f"/api/v1/report?since=7d&workspace_id={added_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(scoped_report["summary"]["events_in_window"], 1)
        self.assertEqual(scoped_report["owner_activity"][0]["owner"], "agent-added")
        self.assertEqual(scoped_report["project_overview"]["summary"]["projects"], 1)

        status, added_issues = self.json_request(
            "GET",
            f"/api/v1/issues?workspace_id={added_id}&limit=100",
        )
        self.assertEqual(status, 200)
        self.assertEqual(added_issues["total"], 1)
        self.assertEqual(added_issues["issues"][0]["kind"], "malformed-event")
        status, original_issues = self.json_request(
            "GET",
            f"/api/v1/issues?workspace_id={original_id}&limit=100",
        )
        self.assertEqual(status, 200)
        self.assertEqual(original_issues["total"], 0)

        status, rejected = self.json_request(
            "POST",
            "/api/v1/workspaces",
            body=json.dumps({"root": "relative/workspace", "max_depth": 0}),
            headers={
                "Content-Type": "application/json",
                "X-Dev-Mesh-Console": "1",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("absolute", rejected["error"])

    def test_rejects_invalid_queries_unknown_paths_and_remote_binding(self) -> None:
        status, result = self.json_request("GET", "/api/v1/events?limit=0")
        self.assertEqual(status, 400)
        self.assertIn("between", result["error"])
        status, result = self.json_request("GET", "/api/v1/events?page=0")
        self.assertEqual(status, 400)
        self.assertIn("page", result["error"])
        status, result = self.json_request("GET", "/api/v1/events?anchor=-1")
        self.assertEqual(status, 400)
        self.assertIn("anchor", result["error"])
        status, _ = self.json_request("GET", "/api/v1/report?limit=101")
        self.assertEqual(status, 400)
        status, result = self.json_request(
            "GET", "/api/v1/report?workspace_id=unknown"
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown workspace_id", result["error"])
        status, result = self.json_request("GET", "/api/v1/graph?limit=9")
        self.assertEqual(status, 400)
        self.assertIn("between", result["error"])
        status, result = self.json_request("GET", "/api/v1/storyline")
        self.assertEqual(status, 400)
        self.assertIn("workspace_id", result["error"])
        status, result = self.json_request(
            "GET", "/api/v1/storyline?workspace_id=unknown&limit=7"
        )
        self.assertEqual(status, 400)
        self.assertIn("between", result["error"])
        status, _ = self.json_request("GET", "/../DESIGN.md")
        self.assertEqual(status, 404)
        status, result = self.json_request(
            "GET",
            "/api/v1/status",
            headers={"Host": f"observer.example:{self.port}"},
        )
        self.assertEqual(status, 421)
        self.assertIn("host", result["error"])
        status, result = self.json_request(
            "POST",
            "/api/v1/collect",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://observer.example:{self.port}",
                "X-Dev-Mesh-Console": "1",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("origin", result["error"])
        with self.assertRaisesRegex(ValueError, "only supports"):
            validate_loopback_host("0.0.0.0")
