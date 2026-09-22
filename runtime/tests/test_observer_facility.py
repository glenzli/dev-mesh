from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from dev_mesh_console.registry import RootRegistry
from dev_mesh_console.state import ConsoleState
from dev_mesh_coord.control_plane import initialize
from dev_mesh_observer.facility_status import (
    ERROR_SCHEMA,
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REQUEST_SCHEMA,
    REQUIRED_REDACTIONS,
    SERVICE_INSTANCE_ID,
    SERVICE_KIND,
    SNAPSHOT_SCHEMA,
    build_facility_snapshot,
)
from dev_mesh_observer.infra_discovery import (
    DISCOVERY_SCHEMA,
    DISCOVERY_VERSION,
    ObserverFacilityService,
    UNIX_SOCKET_BINDING,
)

from helpers import GitWorkspaceTest


def _read_frame(connection: socket.socket) -> dict[str, object]:
    payload = bytearray()
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        payload.extend(chunk)
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise AssertionError("facility response must contain one LF frame")
    return json.loads(payload[:-1].decode("utf-8"))


class ObserverFacilityStatusTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        self.database = Path(self.temporary.name) / "observer.sqlite3"
        self.state = ConsoleState(
            database=self.database,
            registry=RootRegistry(Path(self.temporary.name) / "roots.json", [self.root]),
            max_depth=0,
            collect_interval=60,
        )
        self.state.collect()
        self.service: ObserverFacilityService | None = None
        self.runtime_directory = tempfile.TemporaryDirectory(prefix="dm-fac-", dir="/tmp")
        self.runtime_root = Path(self.runtime_directory.name) / "infra-protocol"

    def tearDown(self) -> None:
        if self.service is not None:
            self.service.stop()
        self.state.close()
        self.runtime_directory.cleanup()
        super().tearDown()

    def _snapshot(self, service: dict[str, str], sequence: int) -> dict[str, object]:
        return build_facility_snapshot(
            database=self.database,
            collector=self.state.status(),
            console_url="http://127.0.0.1:8765/",
            service=service,
            sequence=sequence,
            captured_at=datetime(2026, 8, 13, 4, 35, tzinfo=UTC),
        )

    def _request(self, payload: bytes) -> dict[str, object]:
        assert self.service is not None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.service.socket_path))
            connection.sendall(payload)
            return _read_frame(connection)

    def test_builds_bounded_redacted_snapshot_from_current_catalog(self) -> None:
        identity = {
            "kind": SERVICE_KIND,
            "instance_id": SERVICE_INSTANCE_ID,
            "generation": "gen_0123456789abcdef0123456789abcdef",
        }
        snapshot = self._snapshot(identity, 7)
        self.assertEqual(snapshot["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(snapshot["schema_version"], PROTOCOL_VERSION)
        self.assertEqual(snapshot["service"], identity)
        self.assertEqual(snapshot["sequence"], 7)
        self.assertEqual(snapshot["status"], {"state": "healthy", "reason_codes": []})
        self.assertEqual(
            snapshot["headline_metrics"],
            [
                "dev_mesh.workspaces.available",
                "dev_mesh.collection.pending_events",
                "dev_mesh.contentions.stalled",
            ],
        )
        self.assertEqual(snapshot["redaction"], {"excluded": list(REQUIRED_REDACTIONS)})
        encoded = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(str(self.database), encoded)

        collector = dict(self.state.status())
        collector["last_error"] = f"secret path {self.root}"
        collector["last_result"] = {
            "discovery_issues": [{"message": f"private {self.root}"}],
            "workspaces": [],
        }
        degraded = build_facility_snapshot(
            database=self.database,
            collector=collector,
            console_url="http://127.0.0.1:8765/",
            service=identity,
            sequence=8,
            captured_at=datetime(2026, 8, 13, 4, 35, tzinfo=UTC),
        )
        self.assertEqual(degraded["status"]["state"], "degraded")
        self.assertIn("collection_failed", degraded["status"]["reason_codes"])
        self.assertIn("integrity_issue", degraded["status"]["reason_codes"])
        self.assertNotIn(str(self.root), json.dumps(degraded))

    def test_publishes_registration_and_serves_strict_snapshot_frames(self) -> None:
        self.service = ObserverFacilityService(self._snapshot, runtime_root=self.runtime_root)
        try:
            self.service.start()
        except PermissionError:
            self.skipTest("Unix sockets are unavailable in this sandbox")

        manifest = json.loads(self.service.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), {"schema", "schema_version", "service", "offers"})
        self.assertEqual(manifest["schema"], DISCOVERY_SCHEMA)
        self.assertEqual(manifest["schema_version"], DISCOVERY_VERSION)
        self.assertEqual(manifest["service"], self.service.service)
        self.assertEqual(
            manifest["offers"],
            [
                {
                    "protocol": PROTOCOL_ID,
                    "protocol_versions": [PROTOCOL_VERSION],
                    "binding": UNIX_SOCKET_BINDING,
                    "endpoint": self.service.endpoint,
                }
            ],
        )
        self.assertEqual(stat.S_IMODE(self.service.manifest_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.service.socket_path.stat().st_mode), 0o600)

        request = json.dumps(
            {
                "schema": REQUEST_SCHEMA,
                "schema_version": PROTOCOL_VERSION,
                "operation": "snapshot",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        first = self._request(request)
        second = self._request(request)
        self.assertEqual(first["service"], manifest["service"])
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        invalid = self._request(b'{"schema":"wrong"}\n')
        self.assertEqual(invalid["schema"], ERROR_SCHEMA)
        self.assertEqual(invalid["error"], {"code": "invalid_request"})

    def test_manual_repair_restores_missing_live_registration(self) -> None:
        self.service = ObserverFacilityService(self._snapshot, runtime_root=self.runtime_root)
        try:
            self.service.start()
        except PermissionError:
            self.skipTest("Unix sockets are unavailable in this sandbox")
        expected = json.loads(self.service.manifest_path.read_text(encoding="utf-8"))
        self.service.manifest_path.unlink()

        restored = self.service.repair_publication()
        current = self.service.repair_publication()

        self.assertEqual(restored["publication"], "restored")
        self.assertEqual(current["publication"], "current")
        self.assertEqual(restored["stale_sockets_removed"], 0)
        self.assertEqual(restored["generation"], expected["service"]["generation"])
        self.assertEqual(json.loads(self.service.manifest_path.read_text(encoding="utf-8")), expected)
        self.assertTrue(self.service.socket_path.exists())

    def test_low_frequency_maintenance_restores_deleted_registration(self) -> None:
        reports: list[dict[str, object]] = []
        self.service = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
            maintenance_interval=0.05,
            maintenance_reporter=reports.append,
        )
        try:
            self.service.start()
        except PermissionError:
            self.skipTest("Unix sockets are unavailable in this sandbox")
        self.service.manifest_path.unlink()

        deadline = time.monotonic() + 2
        while not self.service.manifest_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue(self.service.manifest_path.is_file())
        self.assertTrue(any(report.get("publication") == "restored" for report in reports))

    def test_start_removes_only_old_unreferenced_unbound_dev_mesh_sockets(self) -> None:
        self.service = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
            maintenance_interval=0,
            stale_socket_min_age=60,
        )
        stale = self.service.runtime.sockets / "dm-000000000001.sock"
        referenced = self.service.runtime.sockets / "dm-000000000002.sock"
        live = self.service.runtime.sockets / "dm-000000000003.sock"
        young = self.service.runtime.sockets / "dm-000000000004.sock"
        live_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            for path in (stale, referenced, young):
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    listener.bind(str(path))
                    path.chmod(0o600)
                finally:
                    listener.close()
            live_listener.bind(str(live))
        except PermissionError:
            live_listener.close()
            self.skipTest("Unix sockets are unavailable in this sandbox")
        live_listener.listen(1)
        live.chmod(0o600)
        old = time.time() - 120
        for path in (stale, referenced, live):
            os.utime(path, (old, old))
        registration = self.service.runtime.registrations / "other--local.json"
        registration.write_text(
            json.dumps({"offers": [{"endpoint": f"sockets/{referenced.name}"}]}) + "\n",
            encoding="utf-8",
        )
        registration.chmod(0o600)
        try:
            self.service.start()
        except PermissionError:
            live_listener.close()
            self.skipTest("Unix sockets are unavailable in this sandbox")
        try:
            self.assertFalse(stale.exists())
            self.assertTrue(referenced.exists())
            self.assertTrue(live.exists())
            self.assertTrue(young.exists())
        finally:
            live_listener.close()
            for path in (referenced, live, young):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def test_restart_rotates_generation_and_endpoint_but_keeps_manifest(self) -> None:
        self.service = ObserverFacilityService(self._snapshot, runtime_root=self.runtime_root)
        try:
            self.service.start()
        except PermissionError:
            self.skipTest("Unix sockets are unavailable in this sandbox")
        first = json.loads(self.service.manifest_path.read_text(encoding="utf-8"))
        first_socket = self.service.socket_path
        self.service.stop()
        self.assertTrue(self.service.manifest_path.is_file())
        self.assertFalse(first_socket.exists())

        self.service.start()
        second = json.loads(self.service.manifest_path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["service"]["generation"], second["service"]["generation"])
        self.assertNotEqual(first["offers"][0]["endpoint"], second["offers"][0]["endpoint"])


if __name__ == "__main__":
    import unittest

    unittest.main()
