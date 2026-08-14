from __future__ import annotations

import errno
import json
from pathlib import Path

from dev_mesh_coord.constants import (
    AUTHORITY_EFFECTS,
    EVENT_SCHEMA,
    MAX_EVENT_BYTES,
    PROTOCOL,
    PROTOCOL_VERSION,
)
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.errors import ProtocolError, error_json
from dev_mesh_coord.lifecycle import (
    create_claim,
    heartbeat_claim,
    join_run,
    leave_run,
    pause_claim,
    release_claim,
)

from helpers import GitWorkspaceTest, git


class ControlPlaneTest(GitWorkspaceTest):
    def test_exact_markers_and_restart_reuse_one_state(self) -> None:
        first = initialize(self.root)
        second = initialize(self.root)
        self.assertEqual(first.state_root, second.state_root)
        self.assertEqual(first.state_root.name, "20260814.1")
        current = json.loads((self.root / ".dev-mesh/coord/current.json").read_text())
        protocol = json.loads((first.state_root / "protocol.json").read_text())
        self.assertEqual(current["protocol"], PROTOCOL)
        self.assertEqual(current["version"], PROTOCOL_VERSION)
        self.assertEqual(current["event_schema"], EVENT_SCHEMA)
        self.assertEqual(protocol["version"], PROTOCOL_VERSION)
        self.assertFalse(any(path.name.startswith("20260814.1-") for path in first.state_root.parent.iterdir()))

        schema = json.loads((Path(__file__).parents[2] / "schemas/event.schema.json").read_text())
        self.assertEqual(set(schema["properties"]["event"]["enum"]), set(AUTHORITY_EFFECTS))

    def test_live_legacy_requires_cutover(self) -> None:
        (self.root / ".agent-coordination").mkdir()
        with self.assertRaisesRegex(ProtocolError, "explicitly retired"):
            initialize(self.root)

    def test_direct_lifecycle_requires_result_before_dirty_release(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="edit app")
        create_claim(
            self.root,
            scope="app-edit",
            owner="agent-a",
            run_id="run-a",
            task="edit app",
            paths=["app.txt"],
        )
        with self.assertRaisesRegex(ValueError, "active coordination"):
            leave_run(
                self.root,
                run_id="run-a",
                owner="agent-a",
                outcome="completed",
                summary="not yet",
            )
        before = len(list(resolve(self.root).state_root.joinpath("events").glob("*.json")))
        heartbeat = heartbeat_claim(self.root, scope="app-edit", owner="agent-a", run_id="run-a")
        after = len(list(resolve(self.root).state_root.joinpath("events").glob("*.json")))
        self.assertFalse(heartbeat["event_emitted"])
        self.assertEqual(before, after)

        (self.root / "app.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Work Result"):
            release_claim(
                self.root,
                scope="app-edit",
                owner="agent-a",
                run_id="run-a",
                summary="done",
            )
        git(self.root, "add", "app.txt")
        git(self.root, "commit", "-m", "edit app")
        release_claim(
            self.root,
            scope="app-edit",
            owner="agent-a",
            run_id="run-a",
            summary="done",
        )
        closed = leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="done",
        )
        self.assertEqual(closed["status"], "closed")

    def test_marker_disagreement_fails_closed(self) -> None:
        plane = initialize(self.root)
        current = self.root / ".dev-mesh/coord/current.json"
        value = json.loads(current.read_text())
        value["version"] = "20260812.2"
        current.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "unsupported current marker"):
            resolve(self.root)

    def test_environment_failures_keep_stable_non_contention_classification(self) -> None:
        read_only = json.loads(error_json(OSError(errno.EROFS, "read-only filesystem")))
        permission = json.loads(error_json(PermissionError(errno.EACCES, "denied")))
        timeout = json.loads(error_json(TimeoutError("operation lock busy")))
        self.assertEqual(read_only["error"]["code"], "read_only_filesystem")
        self.assertEqual(permission["error"]["code"], "permission_denied")
        self.assertEqual(timeout["error"]["code"], "lock_busy")

    def test_large_nested_overlap_is_compressed_and_events_use_exact_byte_limit(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="nested paths")
        join_run(self.root, run_id="run-b", owner="agent-b", task="nested overlap")
        paths = ["tree/" + "/".join(f"p{part:03d}" for part in range(index + 1)) for index in range(128)]
        create_claim(
            self.root,
            scope="nested-a",
            owner="agent-a",
            run_id="run-a",
            task="nested paths",
            paths=paths,
        )
        pending = create_claim(
            self.root,
            scope="nested-b",
            owner="agent-b",
            run_id="run-b",
            task="nested overlap",
            paths=paths,
            allow_overlap=True,
        )
        self.assertEqual(pending["conflicts"][0]["physical_overlap_count"], 128 * 128)
        self.assertNotIn("paths", pending["conflicts"][0])
        event_paths = list(resolve(self.root).state_root.joinpath("events").glob("*.json"))
        self.assertTrue(event_paths)
        self.assertTrue(all(path.stat().st_size <= MAX_EVENT_BYTES for path in event_paths))

    def test_pause_resource_bound_fails_before_claim_mutation(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="bounded pause")
        create_claim(
            self.root,
            scope="bounded-pause",
            owner="agent-a",
            run_id="run-a",
            task="bounded pause",
            paths=["app.txt"],
        )
        with self.assertRaisesRegex(ValueError, "at most 64 resources"):
            pause_claim(
                self.root,
                scope="bounded-pause",
                owner="agent-a",
                run_id="run-a",
                blocker_kind="dependency",
                checkpoint="before oversized event",
                resume_condition="dependency clears",
                resources=[f"resource-{index}" for index in range(65)],
            )
        claim = json.loads(
            resolve(self.root).state_root.joinpath("claims/bounded-pause.json").read_text()
        )
        self.assertEqual(claim["status"], "active")
