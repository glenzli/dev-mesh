from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from dev_mesh_console.registry import RootRegistry
from dev_mesh_console.server import ConsoleServer, require_loopback_host
from dev_mesh_console.state import ConsoleState
from dev_mesh_coord.control_plane import initialize

from helpers import GitWorkspaceTest


class ConsoleRuntimeTest(GitWorkspaceTest):
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


if __name__ == "__main__":
    import unittest

    unittest.main()
