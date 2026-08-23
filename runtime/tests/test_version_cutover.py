from __future__ import annotations

import json
from unittest import mock

from dev_mesh_coord import version_cutover
from dev_mesh_coord.constants import EVENT_SCHEMA, PROTOCOL_VERSION, STATE_DIRECTORIES
from dev_mesh_coord.control_plane import resolve
from dev_mesh_coord.errors import ProtocolError
from dev_mesh_coord.version_cutover import apply, build_plan, verify

from helpers import GitWorkspaceTest, git


class VersionCutoverTest(GitWorkspaceTest):
    def _old_state(self) -> None:
        namespace = self.root / ".dev-mesh"
        state = namespace / "coord" / "20260814.1"
        state.mkdir(parents=True)
        for relative in STATE_DIRECTORIES:
            (state / relative).mkdir(parents=True, exist_ok=True)
        created = "2026-08-14T00:00:00Z"
        (namespace / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "dev-mesh.workspace",
                    "created_at": created,
                    "coord_current": "coord/current.json",
                }
            )
        )
        (namespace / "coord/current.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "protocol": "dev-mesh.coordination",
                    "version": "20260814.1",
                    "event_schema": 2,
                    "state": "20260814.1",
                    "activated_at": created,
                }
            )
        )
        (state / "protocol.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "protocol": "dev-mesh.coordination",
                    "version": "20260814.1",
                    "event_schema": 2,
                    "created_at": created,
                }
            )
        )
        (state / "runs/old-run.json").write_text(json.dumps({"status": "active"}))
        (state / "claims/old-scope.json").write_text(json.dumps({"status": "active"}))
        (state / "events/old-message.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "event_id": "old-message",
                    "event": "message-sent",
                    "at": created,
                    "protocol": "dev-mesh.coordination",
                    "protocol_version": "20260814.1",
                    "authority_effect": "none",
                    "transaction_id": None,
                    "owner": "agent-old",
                    "run_id": "old-run",
                    "source_owner": "agent-old",
                    "source_run_id": "old-run",
                    "target_owner": "agent-target",
                    "subject": "must not survive retention",
                    "body": "content-bearing old message",
                }
            )
        )

    def test_discards_old_state_after_retention_and_preserves_dirty_baseline(self) -> None:
        self._old_state()
        prior_archive = self.root / ".dev-mesh/coord/archive/older/20260812.1/events"
        prior_archive.mkdir(parents=True)
        prior_archive.joinpath("old.json").write_text("{}", encoding="utf-8")
        (self.root / "app.txt").write_text("base\nshared dirty\n", encoding="utf-8")
        head = git(self.root, "rev-parse", "HEAD")
        plan = build_plan(self.root, cutover_id="upgrade-20260823")
        self.assertEqual(plan["source_disposition"], "discard-after-analysis-retention")
        self.assertEqual(plan["analysis_retention"]["retained_event_count"], 1)
        self.assertTrue(plan["prior_archives"]["present"])
        with self.assertRaisesRegex(ProtocolError, "discard confirmation"):
            apply(
                self.root,
                cutover_id="upgrade-20260823",
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_discard_old_state=False,
            )
        completed = apply(
            self.root,
            cutover_id="upgrade-20260823",
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_discard_old_state=True,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(resolve(self.root).version, PROTOCOL_VERSION)
        self.assertEqual(resolve(self.root).event_schema, EVENT_SCHEMA)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.root, "diff", "--name-only"), "app.txt")
        self.assertFalse(self.root.joinpath(".dev-mesh/coord/20260814.1").exists())
        self.assertFalse(self.root.joinpath(".dev-mesh/coord/archive").exists())
        self.assertFalse(
            self.root.joinpath(
                ".dev-mesh/coord/cutovers/.upgrade-20260823.20260814.1.discarding"
            ).exists()
        )
        retention_path = (
            self.root / ".dev-mesh" / str(plan["analysis_retention"]["path"])
        )
        retention = json.loads(retention_path.read_text())
        self.assertEqual(retention["authority"], "none")
        self.assertEqual(retention["event_counts"], {"message-sent": 1})
        self.assertNotIn("subject", retention["events"][0])
        self.assertNotIn("body", retention["events"][0])
        self.assertTrue(
            verify(
                self.root,
                cutover_id="upgrade-20260823",
                expected_plan_digest=str(plan["plan_digest"]),
            )["verified"]
        )

    def test_cutover_retries_after_source_move_before_journal_transition(self) -> None:
        self._old_state()
        plan = build_plan(self.root, cutover_id="upgrade-retry")
        with mock.patch.object(
            version_cutover, "_transition", side_effect=RuntimeError("stop")
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                apply(
                    self.root,
                    cutover_id="upgrade-retry",
                    expected_plan_digest=str(plan["plan_digest"]),
                    confirm_agents_stopped=True,
                    confirm_discard_old_state=True,
                )
        self.assertFalse(
            self.root.joinpath(".dev-mesh/coord/20260814.1").exists()
        )
        self.assertTrue(
            self.root.joinpath(
                ".dev-mesh/coord/cutovers/.upgrade-retry.20260814.1.discarding"
            ).is_dir()
        )
        completed = apply(
            self.root,
            cutover_id="upgrade-retry",
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_discard_old_state=True,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(
            verify(
                self.root,
                cutover_id="upgrade-retry",
                expected_plan_digest=str(plan["plan_digest"]),
            )["verified"]
        )

    def test_cutover_rejects_unreviewable_id_and_nested_root(self) -> None:
        self._old_state()
        with self.assertRaises(ValueError):
            build_plan(self.root, cutover_id="../outside")
        nested = self.root / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "exact Git workspace root"):
            build_plan(nested, cutover_id="upgrade-nested")

    def test_plan_counts_only_unresolved_old_authority(self) -> None:
        self._old_state()
        state = self.root / ".dev-mesh/coord/20260814.1"
        state.joinpath("runs/closed-run.json").write_text(
            json.dumps({"status": "closed"})
        )
        state.joinpath("handoffs/accepted.json").write_text(
            json.dumps({"status": "accepted"})
        )
        plan = build_plan(self.root, cutover_id="upgrade-inventory")
        self.assertEqual(
            plan["authority_inventory"],
            {
                "runs": 1,
                "claims": 1,
                "handoffs": 0,
                "contentions": 0,
                "work": 0,
                "transactions": 0,
                "direct_commits": 0,
                "cleanups": 0,
            },
        )

    def test_plan_binds_untracked_file_content_not_only_its_name(self) -> None:
        self._old_state()
        untracked = self.root / "new-source.txt"
        untracked.write_text("reviewed\n")
        plan = build_plan(self.root, cutover_id="upgrade-untracked")
        self.assertEqual(plan["git_facts"]["untracked_file_count"], 1)
        untracked.write_text("changed after review\n")
        with self.assertRaisesRegex(ProtocolError, "Git or dirty baseline changed"):
            apply(
                self.root,
                cutover_id="upgrade-untracked",
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_discard_old_state=True,
            )

    def test_verify_rejects_target_protocol_tampering(self) -> None:
        self._old_state()
        plan = build_plan(self.root, cutover_id="upgrade-tamper")
        apply(
            self.root,
            cutover_id="upgrade-tamper",
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_discard_old_state=True,
        )
        protocol_path = self.root / ".dev-mesh/coord" / PROTOCOL_VERSION / "protocol.json"
        protocol = json.loads(protocol_path.read_text())
        protocol["cutover_id"] = "another-cutover"
        protocol_path.write_text(json.dumps(protocol))
        with self.assertRaisesRegex(ProtocolError, "verification failed"):
            verify(
                self.root,
                cutover_id="upgrade-tamper",
                expected_plan_digest=str(plan["plan_digest"]),
            )

    def test_verify_rejects_analysis_retention_tampering(self) -> None:
        self._old_state()
        plan = build_plan(self.root, cutover_id="upgrade-retention-tamper")
        apply(
            self.root,
            cutover_id="upgrade-retention-tamper",
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_discard_old_state=True,
        )
        retention_path = (
            self.root / ".dev-mesh" / str(plan["analysis_retention"]["path"])
        )
        retention = json.loads(retention_path.read_text())
        retention["event_counts"] = {"fabricated": 1}
        retention_path.write_text(json.dumps(retention))
        with self.assertRaisesRegex(ProtocolError, "verification failed"):
            verify(
                self.root,
                cutover_id="upgrade-retention-tamper",
                expected_plan_digest=str(plan["plan_digest"]),
            )
