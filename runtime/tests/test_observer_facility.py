from __future__ import annotations

import json
import socket
import stat
import tempfile
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
