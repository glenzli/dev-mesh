from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.facility_status import (  # noqa: E402
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
from dev_mesh_observer.infra_discovery import (  # noqa: E402
    DISCOVERY_SCHEMA,
    DISCOVERY_VERSION,
    InfraDiscoveryError,
    ObserverFacilityService,
    UNIX_SOCKET_BINDING,
)
from dev_mesh_observer.operations import discover_and_collect  # noqa: E402
from dev_mesh_observer.store import ObserverStore  # noqa: E402


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


class ObserverFacilityStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dm-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "observer-data"
        self.runtime_root = self.root / "infra-protocol"
        self.workspace = self.root / "workspace"
        (self.workspace / ".agent-coordination" / "events").mkdir(parents=True)
        with ObserverStore(self.data_dir) as store:
            discover_and_collect(store, roots=[self.workspace], max_depth=0)
        self.collector = {
            "enabled": True,
            "interval_seconds": 5.0,
            "running": False,
            "cycles": 1,
            "last_attempt_at": "2026-08-12T01:59:59Z",
            "last_success_at": "2026-08-12T02:00:00Z",
            "last_error_at": None,
            "last_error": None,
            "last_result": {
                "collection": {
                    "workspaces": 1,
                    "available": 1,
                    "unavailable": 0,
                    "issues": 0,
                }
            },
        }
        self.service_identity = {
            "kind": SERVICE_KIND,
            "instance_id": SERVICE_INSTANCE_ID,
            "generation": "gen_0123456789abcdef0123456789abcdef",
        }
        self.service: ObserverFacilityService | None = None

    def tearDown(self) -> None:
        if self.service is not None:
            self.service.stop()
        self.temporary.cleanup()

    def _snapshot(self, service: dict[str, str], sequence: int) -> dict[str, object]:
        return build_facility_snapshot(
            data_dir=self.data_dir,
            collector=self.collector,
            console_url="http://127.0.0.1:8765/",
            service=service,
            sequence=sequence,
            captured_at=datetime(2026, 8, 12, 2, 0, 10, tzinfo=UTC),
        )

    def _request(self, payload: bytes) -> dict[str, object]:
        assert self.service is not None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.service.socket_path))
            connection.sendall(payload)
            return _read_frame(connection)

    def test_builds_bounded_redacted_health_snapshot(self) -> None:
        snapshot = self._snapshot(self.service_identity, 7)
        self.assertEqual(snapshot["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(snapshot["schema_version"], PROTOCOL_VERSION)
        self.assertEqual(snapshot["service"], self.service_identity)
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
        self.assertEqual(
            snapshot["redaction"], {"excluded": list(REQUIRED_REDACTIONS)}
        )
        encoded = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn(str(self.workspace), encoded)
        self.assertNotIn(str(self.data_dir), encoded)

        with ObserverStore(self.data_dir) as store:
            workspace_id = str(
                store.connection.execute(
                    "SELECT workspace_id FROM workspaces"
                ).fetchone()[0]
            )
            with store.connection:
                store.connection.execute(
                    """
                    INSERT INTO collection_issues(
                        workspace_id, source_name, kind, detail,
                        observed_digest, first_detected_at, last_detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        "historical.json",
                        "malformed-event",
                        "historical detail must remain private",
                        "digest",
                        "2026-08-12T01:00:00Z",
                        "2026-08-12T01:00:00Z",
                    ),
                )
        historical = self._snapshot(self.service_identity, 8)
        self.assertEqual(historical["status"], {"state": "healthy", "reason_codes": []})
        integrity_metric = next(
            metric
            for metric in historical["metrics"]
            if metric["id"] == "dev_mesh.integrity.issues"
        )
        self.assertEqual(integrity_metric["value"], 1)
        self.assertNotIn("historical detail", json.dumps(historical))

        self.collector["last_error"] = "secret path /private/source"
        self.collector["last_result"] = {"collection": {"issues": 1}}
        degraded = self._snapshot(self.service_identity, 9)
        self.assertEqual(degraded["status"]["state"], "degraded")
        self.assertIn("collection_failed", degraded["status"]["reason_codes"])
        self.assertIn("integrity_issue", degraded["status"]["reason_codes"])
        self.assertNotIn("secret path", json.dumps(degraded))

    def test_publishes_registration_and_serves_strict_snapshot_frames(self) -> None:
        self.service = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
        )
        self.service.start()

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
        self.assertEqual(
            stat.S_IMODE(self.service.manifest_path.stat().st_mode), 0o600
        )
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
        self.assertEqual(first["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(first["service"], manifest["service"])
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)

        invalid = self._request(b'{"schema":"wrong"}\n')
        self.assertEqual(invalid["schema"], ERROR_SCHEMA)
        self.assertEqual(invalid["error"], {"code": "invalid_request"})

        original_payload = self.service.manifest_path.read_bytes()
        original_mtime = self.service.manifest_path.stat().st_mtime_ns
        time.sleep(0.2)
        self.assertEqual(self.service.manifest_path.read_bytes(), original_payload)
        self.assertEqual(self.service.manifest_path.stat().st_mtime_ns, original_mtime)

    def test_serializes_publication_and_replaces_manifest_on_restart(self) -> None:
        self.service = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
        )
        self.service.start()
        manifest_path = self.service.manifest_path
        socket_path = self.service.socket_path
        first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        competing = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
        )
        with self.assertRaises(InfraDiscoveryError):
            competing.start()
        competing.stop()

        self.service.stop()
        self.service = None
        self.assertTrue(manifest_path.is_file())
        self.assertFalse(socket_path.exists())

        successor = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
        )
        successor.start()
        self.service = successor
        replacement = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(replacement["schema_version"], DISCOVERY_VERSION)
        self.assertNotEqual(
            replacement["service"]["generation"],
            first_manifest["service"]["generation"],
        )
        self.assertNotEqual(
            replacement["offers"][0]["endpoint"],
            first_manifest["offers"][0]["endpoint"],
        )
        self.assertTrue(successor.socket_path.exists())

    def test_does_not_misreport_lock_open_failure_as_an_active_publisher(self) -> None:
        self.service = ObserverFacilityService(
            self._snapshot,
            runtime_root=self.runtime_root,
        )
        with patch(
            "dev_mesh_observer.infra_discovery.os.open",
            side_effect=PermissionError(13, "permission denied"),
        ):
            with self.assertRaisesRegex(
                InfraDiscoveryError,
                "cannot open Observer publication authority lock",
            ):
                self.service.start()

    def test_retries_endpoint_collision_without_removing_the_existing_socket(self) -> None:
        generation = uuid.UUID("11111111-1111-1111-1111-111111111111")
        collision = uuid.UUID("22222222-2222-2222-2222-222222222222")
        retry = uuid.UUID("33333333-3333-3333-3333-333333333333")
        temporary = uuid.UUID("44444444-4444-4444-4444-444444444444")
        with patch(
            "dev_mesh_observer.infra_discovery.uuid.uuid4",
            side_effect=[generation, collision, retry, temporary],
        ):
            self.service = ObserverFacilityService(
                self._snapshot,
                runtime_root=self.runtime_root,
            )
            collided_path = self.service.socket_path
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
                existing.bind(str(collided_path))
                existing.listen(1)
                self.service.start()
                self.assertTrue(collided_path.exists())
                self.assertNotEqual(self.service.socket_path, collided_path)
                self.assertEqual(
                    self.service.endpoint,
                    "sockets/dm-333333333333.sock",
                )
            collided_path.unlink()


if __name__ == "__main__":
    unittest.main()
