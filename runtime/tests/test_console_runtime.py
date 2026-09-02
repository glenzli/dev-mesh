from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.client import HTTPConnection
from pathlib import Path

from dev_mesh_console.registry import RootRegistry
from dev_mesh_console.server import ConsoleServer, require_loopback_host
from dev_mesh_console.state import ConsoleState
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import join_run

from helpers import GitWorkspaceTest


class ConsoleRuntimeTest(GitWorkspaceTest):
    def test_console_brand_uses_packaged_png_icon(self) -> None:
        web_root = Path(__file__).parents[1] / "dev_mesh_console" / "web"
        icon = (web_root / "dev-mesh-icon.png").read_bytes()
        html = (web_root / "index.html").read_text(encoding="utf-8")

        self.assertEqual(icon[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(icon[16:20]), 128)
        self.assertEqual(int.from_bytes(icon[20:24]), 128)
        self.assertIn('rel="icon" type="image/png" href="/dev-mesh-icon.png"', html)
        self.assertIn('class="brand-mark" src="/dev-mesh-icon.png"', html)

    def test_tooltip_position_tracks_the_scrolled_viewport(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        module = (
            Path(__file__).parents[1]
            / "dev_mesh_console"
            / "web"
            / "flow_layout.js"
        ).as_uri()
        script = f"""
          import {{ activeRunKeys, buildFlowLayout, identityKey, timeLabelMode, tooltipPosition, transactionBranchOffset }} from {json.dumps(module)};
          const scrolled = tooltipPosition(
            {{ x: 1780, y: 42 }},
            {{ scrollLeft: 1200, clientWidth: 800, tooltipWidth: 250 }},
          );
          if (scrolled.left !== 1742 || scrolled.top !== 54) {{
            throw new Error(`unexpected scrolled position ${{JSON.stringify(scrolled)}}`);
          }}
          const unscrolled = tooltipPosition(
            {{ x: 100, y: 20 }},
            {{ scrollLeft: 0, clientWidth: 800, tooltipWidth: 250 }},
          );
          if (unscrolled.left !== 116 || unscrolled.top !== 32) {{
            throw new Error(`unexpected initial position ${{JSON.stringify(unscrolled)}}`);
          }}
          const layout = buildFlowLayout([
            {{event_id: "a", owner: "agent-a", run_id: "run-one", at: "2026-08-14T08:00:00Z"}},
            {{event_id: "b", owner: "agent-a", run_id: "run-two", at: "2026-08-14T08:00:01Z"}},
            {{event_id: "c", owner: "agent-a", run_id: "run-one", at: "2026-08-14T08:00:02Z"}},
          ]);
          const row = layout.ownerRows[0];
          if (row.height < 96 || layout.timeAxisY + 4 >= row.top) {{
            throw new Error(`timeline ruler collides with lane ${{JSON.stringify({{row, timeAxisY: layout.timeAxisY}})}}`);
          }}
          const relationLayout = buildFlowLayout([{{
            event_id: "contention",
            event: "contention-opened",
            owner: "agent-a",
            run_id: "run-a",
            at: "2026-08-14T08:00:03Z",
            details: {{contention_participants: [
              {{owner: "agent-a", run_id: "run-a", scope: "left"}},
              {{owner: "agent-b", run_id: "run-b", scope: "right"}},
            ]}},
          }}]);
          const referenced = relationLayout.ownerRows.find((item) => item.owner === "agent-b");
          if (!referenced?.ownerOnly || JSON.stringify(referenced.referencedRunIds) !== JSON.stringify(["run-b"])) {{
            throw new Error(`referenced participant Run was lost ${{JSON.stringify(relationLayout.ownerRows)}}`);
          }}
          if (transactionBranchOffset({{event: "transaction-created"}}) !== 15
              || transactionBranchOffset({{event: "transaction-published"}}) !== 15
              || transactionBranchOffset({{event: "claim-created"}}) !== 0) {{
            throw new Error("transaction branch offsets must remain continuous");
          }}
          if (timeLabelMode(238, 410) !== "range" || timeLabelMode(238, 520) !== "endpoints") {{
            throw new Error("dense time labels must collapse into one range label");
          }}
          if (identityKey("active-agent", "run-1") !== "active-agent\\u0000run-1") {{
            throw new Error("active Run identity must match flow lanes exactly");
          }}
          const activeRuns = activeRunKeys([
            {{kind: "run", status: "active", owner: "active-agent", run_id: "run-1"}},
            {{kind: "claim", status: "active", owner: "active-agent", run_id: "run-1"}},
            {{kind: "run", status: "closed", owner: "closed-agent", run_id: "run-2"}},
          ]);
          if (!activeRuns.has(identityKey("active-agent", "run-1")) || activeRuns.size !== 1) {{
            throw new Error("only active Runs may animate flow start nodes");
          }}
        """
        subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_project_collaboration_graph_and_project_filtering(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        module = (
            Path(__file__).parents[1]
            / "dev_mesh_console"
            / "web"
            / "project_overview.js"
        ).as_uri()
        script = f"""
          import {{ projectGraphLayout, relationLabel, selectableProjects }} from {json.dumps(module)};
          const projects = [
            {{workspace_id: "quiet", name: "quiet", event_count: 0, event_counts: {{}}, active: {{}}, diagnostic_count: 0}},
            {{workspace_id: "base", name: "base", event_count: 2, event_counts: {{"agent-joined": 1, "claim-created": 1}}, active: {{}}, diagnostic_count: 0}},
            {{workspace_id: "collab", name: "collab", event_count: 7, event_counts: {{"contention-opened": 2, "message-sent": 3, "transaction-created": 1, "work-suspended": 1}}, active: {{}}, diagnostic_count: 0}},
          ];
          const selectable = selectableProjects(projects).map((project) => project.workspace_id);
          if (JSON.stringify(selectable) !== JSON.stringify(["base", "collab"])) {{
            throw new Error(`unexpected options ${{JSON.stringify(selectable)}}`);
          }}
          const retained = selectableProjects(projects, "quiet").map((project) => project.workspace_id);
          if (!retained.includes("quiet")) throw new Error("selected quiet project was dropped");
          const layout = projectGraphLayout({{
            nodes: [
              {{workspace_id: "left", name: "left"}},
              {{workspace_id: "right", name: "right"}},
              {{workspace_id: "unrelated", name: "unrelated"}},
            ],
            edges: [{{
              source_workspace_id: "left",
              target_workspace_id: "right",
              collaboration_count: 1,
              open_collaboration_count: 1,
              directions: [],
            }}],
          }});
          if (layout.nodes.length !== 2 || layout.edges.length !== 1) {{
            throw new Error(`unexpected project graph ${{JSON.stringify(layout)}}`);
          }}
          if (layout.nodes[0].x >= layout.nodes[1].x || !layout.edges[0].path.startsWith("M ")) {{
            throw new Error(`project graph was not laid out left-to-right ${{JSON.stringify(layout)}}`);
          }}
          if (!layout.edges[0].protocol || !layout.edges[0].direct) {{
            throw new Error(`explicit collaboration was not projected as direct ${{JSON.stringify(layout)}}`);
          }}
          const closedLabel = relationLabel({{
            collaboration_count: 3, active_collaboration_count: 1, pending_settlement_count: 1,
          }}, (key) => key, String);
          if (!closedLabel.includes("projectOverview.closed 1") || !closedLabel.includes("projectOverview.pendingSettlement 1")) {{
            throw new Error(`closed and pending relations were conflated ${{closedLabel}}`);
          }}
          const hintLayout = projectGraphLayout({{
            nodes: [
              {{workspace_id: "left", name: "left"}},
              {{workspace_id: "middle", name: "middle"}},
              {{workspace_id: "right", name: "right"}},
            ],
            edges: [],
            hint_groups: [{{
              workspace_ids: ["left", "middle", "right"],
              same_run_hint_count: 1,
              samples: [{{owner: "agent-a", run_id: "run-a"}}],
            }}],
          }});
          if (hintLayout.nodes.length !== 3 || hintLayout.edges.length !== 0 || hintLayout.hintGroups.length !== 1) {{
            throw new Error(`same-run hint was not grouped ${{JSON.stringify(hintLayout)}}`);
          }}
          if (hintLayout.hintGroups[0].project_count !== 3 || !hintLayout.hintGroups[0].path.includes(" H ")) {{
            throw new Error(`same-run hint group lacks one multi-project bracket ${{JSON.stringify(hintLayout)}}`);
          }}
          const evidenceOnly = projectGraphLayout({{
            nodes: [{{workspace_id: "left"}}, {{workspace_id: "right"}}, {{workspace_id: "hint-only"}}],
            edges: [{{source_workspace_id: "left", target_workspace_id: "right", collaboration_count: 1}}],
            hint_groups: [{{workspace_ids: ["left", "hint-only"], same_run_hint_count: 1}}],
          }}, {{includeHints: false}});
          if (evidenceOnly.nodes.length !== 2 || evidenceOnly.hintGroups.length) {{
            throw new Error("history must not draw same-name hints as project relationships");
          }}
        """
        subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_dashboard_presentation_collapses_independent_work(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        module = (
            Path(__file__).parents[1]
            / "dev_mesh_console"
            / "web"
            / "dashboard_presentation.js"
        ).as_uri()
        script = f"""
          import {{ contentionSummaries, dashboardPresentation }} from {json.dumps(module)};
          const quietDashboard = {{
            coordination: {{event_count: 0, independent_run_count: 5, total_run_count: 5}},
            project_collaboration: {{collaboration_relation_count: 0, inferred_relation_count: 1, relation_count: 1}},
            projects: [{{workspace_id: "site", name: "site"}}],
            active_details: [
              {{kind: "run", workspace_id: "site", run_id: "run-1"}},
              {{kind: "claim", workspace_id: "site", run_id: "run-1"}},
            ],
          }};
          const selected = dashboardPresentation(quietDashboard, "site");
          if (!selected.quiet || selected.showProjects || selected.showFlow || selected.showProjectRelations) {{
            throw new Error(`independent work did not collapse ${{JSON.stringify(selected)}}`);
          }}
          if (selected.activeRunCount !== 1 || selected.independentRunCount !== 5) {{
            throw new Error(`quiet work counts changed ${{JSON.stringify(selected)}}`);
          }}
          const overview = dashboardPresentation(quietDashboard);
          if (!overview.showProjects) throw new Error("all-project overview lost project navigation");
          const empty = dashboardPresentation({{
            coordination: {{event_count: 0}},
            project_collaboration: {{collaboration_relation_count: 0}},
            projects: [],
            active_details: [],
          }});
          if (empty.quiet || !empty.showProjects) throw new Error("missing projects were presented as healthy quiet work");
          const openedEvent = {{
            event: "contention-opened",
            contention_id: "contention-1",
            workspace_id: "site",
            at: "2026-08-29T14:19:16Z",
            details: {{contention_participants: [
              {{owner: "agent-a", run_id: "run-a", scope: "src/a.js"}},
              {{owner: "agent-b", run_id: "run-b", scope: "src/b.js"}},
            ]}},
          }};
          const activeDashboard = {{
            ...quietDashboard,
            operational: {{contention: {{hot_paths: [{{path: "src/a.js"}}, {{path: "src/b.js"}}]}}}},
            coordination: {{event_count: 1, relation_count: 1, events: [openedEvent]}},
            active_details: [{{kind: "contention", workspace_id: "site", object_id: "contention-1"}}],
          }};
          const contention = dashboardPresentation(activeDashboard, "site");
          if (contention.quiet || !contention.attention || !contention.showWorkbench) {{
            throw new Error(`active contention was not actionable ${{JSON.stringify(contention)}}`);
          }}
          if (!contention.showFlow || contention.showProjects || contention.affectedRunCount !== 2 || contention.requestedPathCount !== 2) {{
            throw new Error(`single recorded conflict was hidden ${{JSON.stringify(contention)}}`);
          }}
          if (!contention.defaultOpenFlow) throw new Error("single conflict event was collapsed");
          const activeOutsideWindow = dashboardPresentation({{
            ...quietDashboard,
            operational: {{contention: {{hot_paths: []}}}},
            coordination: {{event_count: 0, relation_count: 0, events: []}},
            active_details: [{{
              kind: "contention",
              workspace_id: "site",
              object_id: "contention-before-window",
              details: {{contention_participants: openedEvent.details.contention_participants}},
            }}],
          }}, "site");
          if (!activeOutsideWindow.attention || !activeOutsideWindow.showWorkbench || activeOutsideWindow.affectedRunCount !== 2) {{
            throw new Error(`active contention outside the event window lost its participants ${{JSON.stringify(activeOutsideWindow)}}`);
          }}
          const investigation = dashboardPresentation({{
            ...activeDashboard,
            coordination: {{
              event_count: 2,
              relation_count: 1,
              events: [openedEvent, {{...openedEvent, event: "contention-proposed", at: "2026-08-29T14:20:00Z"}}],
            }},
          }}, "site");
          if (!investigation.showFlow || !investigation.defaultOpenFlow) {{
            throw new Error("active multi-event investigation did not open by default");
          }}
          const resolvedDashboard = {{
            ...activeDashboard,
            active_details: [],
            coordination: {{
              event_count: 2,
              relation_count: 1,
              events: [openedEvent, {{
                ...openedEvent,
                event: "contention-cancelled",
                at: "2026-08-29T14:21:11Z",
                details: {{...openedEvent.details, reason_code: "superseded"}},
              }}],
            }},
          }};
          const resolved = dashboardPresentation(resolvedDashboard, "site");
          const summaries = contentionSummaries(resolvedDashboard);
          if (resolved.attention || !resolved.showWorkbench || !resolved.showFlow) {{
            throw new Error(`resolved contention state was lost ${{JSON.stringify(resolved)}}`);
          }}
          if (!resolved.defaultOpenFlow) throw new Error("resolved contention history was collapsed by default");
          if (summaries.length !== 1 || summaries[0].status !== "cancelled" || summaries[0].reasonCode !== "superseded") {{
            throw new Error(`terminal contention was not summarized ${{JSON.stringify(summaries)}}`);
          }}
        """
        subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_history_is_independent_of_current_contention(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        web = Path(__file__).parents[1] / "dev_mesh_console" / "web"
        module = (web / "dashboard_presentation.js").as_uri()
        layout_module = (web / "flow_layout.js").as_uri()
        script = f"""
          import {{ contentionSummaries, dashboardPresentation }} from {json.dumps(module)};
          import {{ activeRunKeys, buildFlowLayout, identityKey, ownerKey }} from {json.dumps(layout_module)};
          for (const event of ["message-sent", "handoff-accepted", "work-resumed", "run-authority-recovered"]) {{
            const dashboard = {{
              projects: [{{workspace_id: "a"}}], active_details: [],
              coordination: {{event_count: 1, relation_count: 1, events: [{{event, object_id: "not-a-contention"}}]}},
            }};
            const view = dashboardPresentation(dashboard, "a");
            if (!view.showFlow || !view.defaultOpenFlow || view.attention || view.showWorkbench) {{
              throw new Error(`historical ${{event}} did not remain visible without contention: ${{JSON.stringify(view)}}`);
            }}
            if (contentionSummaries(dashboard).length) throw new Error("an interaction became a contention");
          }}
          const crossProjectDashboard = {{
            projects: [{{workspace_id: "a"}}], active_details: [],
            coordination: {{event_count: 0, relation_count: 0}},
            project_collaboration: {{
              collaboration_relation_count: 1,
              nodes: [{{workspace_id: "a"}}, {{workspace_id: "b"}}],
              edges: [{{source_workspace_id: "a", target_workspace_id: "b", collaboration_count: 1}}],
            }},
          }};
          const crossProjectOnly = dashboardPresentation(crossProjectDashboard);
          if (!crossProjectOnly.showProjectRelations || crossProjectOnly.showFlow || !crossProjectOnly.defaultOpenFlow) {{
            throw new Error("closed cross-project history depends on local contention");
          }}
          if (dashboardPresentation(crossProjectDashboard, "unrelated").showProjectRelations
              || !dashboardPresentation(crossProjectDashboard, "a").showProjectRelations
              || !dashboardPresentation(crossProjectDashboard, "b").showProjectRelations) {{
            throw new Error("history did not scope cross-project evidence to both endpoints");
          }}
          const noEvidence = dashboardPresentation({{
            projects: [{{workspace_id: "a"}}], active_details: [],
            coordination: {{event_count: 0, relation_count: 0}},
            project_collaboration: {{collaboration_relation_count: 0, inferred_relation_count: 2}},
          }});
          if (noEvidence.showFlow || noEvidence.showProjectRelations || !noEvidence.defaultOpenFlow) {{
            throw new Error("empty history must stay reachable without inventing relationships");
          }}
          const event = {{
            event_id: "a", event: "message-sent", workspace_id: "a", owner: "owner", run_id: "run",
            at: "2026-09-03T08:00:00Z", details: {{source_owner: "owner", source_run_id: "run", target_owner: "peer", target_run_id: "peer-run"}},
          }};
          const layout = buildFlowLayout([event, {{...event, event_id: "b", workspace_id: "b"}}]);
          if (layout.runLanes.length !== 2 || layout.ownerRows.length !== 4 || layout.groupCount !== 2) {{
            throw new Error("matching names across workspaces were merged into collaboration");
          }}
          for (const workspaceId of ["a", "b"]) {{
            if (!layout.runPositions.has(identityKey("owner", "run", workspaceId))
                || !layout.ownerRows.some((row) => row.key === ownerKey("peer", workspaceId))) {{
              throw new Error("workspace-scoped event or referenced peer was lost");
            }}
          }}
          const active = activeRunKeys([{{kind: "run", status: "active", owner: "owner", run_id: "run", workspace_id: "a"}}]);
          if (!active.has(identityKey("owner", "run", "a")) || active.has(identityKey("owner", "run", "b"))) {{
            throw new Error("current status leaked between projects");
          }}
        """
        subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )
        html = (web / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="flow-panel" class="panel flow-panel" open', html)
        self.assertLess(html.index('id="work-status-grid"'), html.index('id="flow-panel"'))
        self.assertLess(html.index('id="flow-panel"'), html.index('id="project-collaboration-panel"'))

    def test_root_registry_is_durable_bounded_external_state(self) -> None:
        registry_path = Path(self.temporary.name) / "console-roots.json"
        registry = RootRegistry(registry_path, [self.root])
        self.assertEqual(registry.roots(), [self.root.resolve()])
        self.assertEqual(RootRegistry(registry_path).roots(), [self.root.resolve()])
        self.assertEqual(registry_path.stat().st_mode & 0o777, 0o600)
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"roots": [str(self.root.resolve())], "schema": 1})

        registry.remove(self.root)
        self.assertEqual(RootRegistry(registry_path).roots(), [])
        with self.assertRaisesRegex(ValueError, "outside coordination state"):
            RootRegistry(self.root / ".dev-mesh" / "roots.json")

    def test_state_collects_registered_workspaces_and_exposes_status(self) -> None:
        initialize(self.root)
        registry = RootRegistry(Path(self.temporary.name) / "roots.json", [self.root])
        state = ConsoleState(
            database=Path(self.temporary.name) / "observer.sqlite3",
            registry=registry,
            max_depth=0,
            collect_interval=60,
        )
        try:
            result = state.collect()
            status = state.status()
            dashboard = state.dashboard(workspace=None, window_hours=48, event_limit=20)
        finally:
            state.close()

        self.assertEqual(result["workspace_count"], 1)
        self.assertFalse(status["collecting"])
        self.assertEqual(status["cycles"], 1)
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["roots"], [str(self.root.resolve())])
        self.assertEqual(len(dashboard["projects"]), 1)
        self.assertEqual(dashboard["collector"]["last_result"]["workspace_count"], 1)

    def test_console_bind_address_must_be_literal_loopback(self) -> None:
        self.assertEqual(require_loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(require_loopback_host("::1"), "::1")
        for value in ("0.0.0.0", "192.0.2.1", "localhost"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    require_loopback_host(value)

    def test_runtime_limits_fail_before_collection(self) -> None:
        registry = RootRegistry(Path(self.temporary.name) / "roots.json")
        with self.assertRaisesRegex(ValueError, "max depth"):
            ConsoleState(
                database=Path(self.temporary.name) / "observer.sqlite3",
                registry=registry,
                max_depth=13,
                collect_interval=15,
            )
        with self.assertRaisesRegex(ValueError, "collect interval"):
            ConsoleState(
                database=Path(self.temporary.name) / "observer.sqlite3",
                registry=registry,
                max_depth=5,
                collect_interval=0,
            )

    def test_loopback_http_serves_dashboard_and_rejects_foreign_host(self) -> None:
        initialize(self.root)
        registry = RootRegistry(Path(self.temporary.name) / "roots.json", [self.root])
        state = ConsoleState(
            database=Path(self.temporary.name) / "observer.sqlite3",
            registry=registry,
            max_depth=0,
            collect_interval=60,
        )
        state.collect()
        try:
            server = ConsoleServer("127.0.0.1", 0, state)
        except PermissionError:
            state.close()
            self.skipTest("loopback sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/dashboard?window=48&limit=20")
            response = connection.getresponse()
            dashboard = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(dashboard["kind"], "dev-mesh.console.dashboard")
            self.assertEqual(len(dashboard["projects"]), 1)
            self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
            connection.close()

            rejected = HTTPConnection("127.0.0.1", port, timeout=5)
            rejected.putrequest("GET", "/api/health", skip_host=True)
            rejected.putheader("Host", "example.invalid")
            rejected.endheaders()
            denied = rejected.getresponse()
            value = json.loads(denied.read())
            self.assertEqual(denied.status, 403)
            self.assertEqual(value["error"]["code"], "origin_rejected")
            rejected.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_loopback_reviewed_close_uses_exact_materialized_preview(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-review", owner="agent-a", task="finished work")
        registry = RootRegistry(Path(self.temporary.name) / "roots.json", [self.root])
        state = ConsoleState(
            database=Path(self.temporary.name) / "observer.sqlite3",
            registry=registry,
            max_depth=0,
            collect_interval=60,
        )
        state.collect()
        try:
            server = ConsoleServer("127.0.0.1", 0, state)
        except PermissionError:
            state.close()
            self.skipTest("loopback sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            workspace_id = state.dashboard(
                workspace=None,
                window_hours=48,
                event_limit=20,
            )["projects"][0]["workspace_id"]
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST",
                "/api/actions/run-close/preview",
                body=json.dumps({"workspace_id": workspace_id, "run_id": "run-review"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            preview = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(preview["owner"], "agent-a")
            self.assertFalse(preview["authority_preserved"])

            connection.request(
                "POST",
                "/api/actions/run-close",
                body=json.dumps(
                    {
                        "workspace_id": workspace_id,
                        "run_id": "run-review",
                        "review_token": preview["review_token"],
                        "reviewer": "local-operator",
                        "outcome": "completed",
                        "reason_code": "reviewed-complete",
                        "evidence": "Reviewed the completed work and missing terminal leave.",
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            closed_response = connection.getresponse()
            closed = json.loads(closed_response.read())
            self.assertEqual(closed_response.status, 200)
            self.assertEqual(closed["run"]["status"], "closed")
            self.assertTrue(closed["collection"]["refreshed"])
            connection.close()
            snapshot = json.loads(
                (resolve(self.root).state_root / "runs/run-review.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(snapshot["operator_review"]["reviewer"], "local-operator")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_loopback_discovery_repair_invokes_bound_publisher_once(self) -> None:
        registry = RootRegistry(Path(self.temporary.name) / "roots.json")
        calls: list[bool] = []
        state = ConsoleState(
            database=Path(self.temporary.name) / "observer.sqlite3",
            registry=registry,
            max_depth=0,
            collect_interval=60,
            discovery_repair=lambda: calls.append(True) or {"publication": "restored"},
        )
        try:
            server = ConsoleServer("127.0.0.1", 0, state)
        except PermissionError:
            state.close()
            self.skipTest("loopback sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST",
                "/api/actions/discovery/repair",
                body="{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result, {"publication": "restored"})
            self.assertEqual(calls, [True])

            connection.request(
                "POST",
                "/api/actions/discovery/repair",
                body=json.dumps({"unexpected": True}),
                headers={"Content-Type": "application/json"},
            )
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 400)
            self.assertEqual(calls, [True])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    import unittest

    unittest.main()
