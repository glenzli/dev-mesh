from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from dev_mesh_coord import control_plane
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.cutover import apply, build_plan, tree_digest, verify, write_plan
from dev_mesh_coord.lifecycle import create_claim, join_run
from dev_mesh_observer.catalog import Catalog, discover_workspaces, workspace_id

from helpers import GitWorkspaceTest


class CutoverTest(GitWorkspaceTest):
    def _legacy(self) -> Path:
        legacy = self.root / ".agent-coordination"
        (legacy / "claims").mkdir(parents=True)
        (legacy / "runs").mkdir()
        (legacy / "claims/active.json").write_text('{"status":"active"}\n', encoding="utf-8")
        return legacy

    def test_fresh_start_cutover_preserves_git_and_archives_legacy(self) -> None:
        legacy = self._legacy()
        (self.root / "other.txt").write_text("dirty but preserved\n", encoding="utf-8")
        before = (self.root / "other.txt").read_bytes()
        archive_root = Path(self.temporary.name) / "archives"
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(self.root, archive_root=archive_root)
        self.assertEqual(plan["target_version"], "20260814.1")
        self.assertEqual(plan["legacy_inventory"]["active_object_files"]["claims"], 1)
        write_plan(journal, plan)
        with self.assertRaisesRegex(ValueError, "active authority objects"):
            apply(
                journal,
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_no_legacy_writers=True,
            )
        result = apply(
            journal,
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_no_legacy_writers=True,
            confirm_retire_active_authority=True,
        )
        self.assertEqual(result["state"], "completed")
        self.assertEqual((self.root / "other.txt").read_bytes(), before)
        archive = Path(str(plan["archive_path"]))
        self.assertEqual(tree_digest(archive), plan["legacy_digest"])
        self.assertEqual(list((self.root / ".agent-coordination").iterdir()), [self.root / ".agent-coordination/TOMBSTONE.json"])
        self.assertEqual(resolve(self.root).version, "20260814.1")
        self.assertTrue(verify(journal, expected_plan_digest=str(plan["plan_digest"]))["verified"])

    def test_effect_ahead_of_journal_is_reconciled(self) -> None:
        legacy = self._legacy()
        archive_root = Path(self.temporary.name) / "archives"
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(self.root, archive_root=archive_root)
        write_plan(journal, plan)
        archive = Path(str(plan["archive_path"]))
        archive.parent.mkdir(parents=True)
        os.replace(legacy, archive)
        result = apply(
            journal,
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_no_legacy_writers=True,
            confirm_retire_active_authority=True,
        )
        self.assertEqual(result["state"], "completed")

    def test_effect_ahead_rejects_current_state_from_another_cutover(self) -> None:
        legacy = self._legacy()
        archive_root = Path(self.temporary.name) / "archives"
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(self.root, archive_root=archive_root)
        write_plan(journal, plan)
        archive = Path(str(plan["archive_path"]))
        archive.parent.mkdir(parents=True)
        os.replace(legacy, archive)
        initialize(self.root, cutover_id="cutover-other")
        with self.assertRaisesRegex(ValueError, "another cutover"):
            apply(
                journal,
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_no_legacy_writers=True,
                confirm_retire_active_authority=True,
            )

    def test_unclassified_legacy_record_requires_explicit_retirement(self) -> None:
        legacy = self.root / ".agent-coordination"
        (legacy / "claims").mkdir(parents=True)
        (legacy / "claims/invalid.json").write_text("not-json\n", encoding="utf-8")
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(
            self.root,
            archive_root=Path(self.temporary.name) / "archives",
        )
        self.assertEqual(plan["legacy_inventory"]["invalid_json_records"], 1)
        write_plan(journal, plan)
        with self.assertRaisesRegex(ValueError, "unclassified records"):
            apply(
                journal,
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_no_legacy_writers=True,
            )

    def test_unknown_legacy_status_is_unclassified_and_requires_retirement(self) -> None:
        legacy = self.root / ".agent-coordination"
        (legacy / "claims").mkdir(parents=True)
        (legacy / "claims/future.json").write_text(
            '{"status":"future-active"}\n', encoding="utf-8"
        )
        plan = build_plan(
            self.root,
            archive_root=Path(self.temporary.name) / "archives",
        )
        self.assertEqual(plan["legacy_inventory"]["invalid_json_records"], 1)

    def test_nested_workspace_path_is_rejected_before_legacy_retirement(self) -> None:
        nested = self.root / "nested"
        legacy = nested / ".agent-coordination"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exact Git workspace root"):
            build_plan(
                nested,
                archive_root=Path(self.temporary.name) / "archives",
            )
        self.assertTrue(legacy.exists())

    def test_verify_binds_tombstone_to_exact_archive_path(self) -> None:
        self._legacy()
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(
            self.root,
            archive_root=Path(self.temporary.name) / "archives",
        )
        write_plan(journal, plan)
        apply(
            journal,
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_no_legacy_writers=True,
            confirm_retire_active_authority=True,
        )
        tombstone = self.root / ".agent-coordination/TOMBSTONE.json"
        value = json.loads(tombstone.read_text(encoding="utf-8"))
        value["archive_path"] = str(Path(self.temporary.name) / "wrong-archive")
        tombstone.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cutover facts do not agree"):
            verify(journal, expected_plan_digest=str(plan["plan_digest"]))

    def test_empty_tombstone_staging_is_reused_after_interruption(self) -> None:
        legacy = self._legacy()
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(
            self.root,
            archive_root=Path(self.temporary.name) / "archives",
        )
        write_plan(journal, plan)
        original_replace = control_plane.replace_json

        def fail_before_marker(
            path: Path, value: dict[str, object], **kwargs: object
        ) -> None:
            if path.name == "TOMBSTONE.json":
                raise RuntimeError("injected stop before tombstone marker")
            original_replace(path, value, **kwargs)

        with mock.patch.object(
            control_plane, "replace_json", side_effect=fail_before_marker
        ):
            with self.assertRaisesRegex(RuntimeError, "before tombstone marker"):
                apply(
                    journal,
                    expected_plan_digest=str(plan["plan_digest"]),
                    confirm_agents_stopped=True,
                    confirm_no_legacy_writers=True,
                    confirm_retire_active_authority=True,
                )
        self.assertFalse(legacy.exists())
        staging = (
            self.root
            / ".dev-mesh/coord/cutovers"
            / f".{plan['cutover_id']}.legacy-tombstone-staging"
        )
        self.assertTrue(staging.is_dir())
        self.assertEqual(list(staging.iterdir()), [])

        completed = apply(
            journal,
            expected_plan_digest=str(plan["plan_digest"]),
            confirm_agents_stopped=True,
            confirm_no_legacy_writers=True,
            confirm_retire_active_authority=True,
        )
        self.assertEqual(completed["state"], "completed")
        self.assertFalse(staging.exists())
        self.assertTrue((legacy / "TOMBSTONE.json").is_file())

    def test_archive_symlink_drift_is_rejected_before_legacy_move(self) -> None:
        legacy = self._legacy()
        archive_root = Path(self.temporary.name) / "reviewed-archives"
        journal = Path(self.temporary.name) / "cutover.json"
        plan = build_plan(self.root, archive_root=archive_root)
        write_plan(journal, plan)
        diverted = Path(self.temporary.name) / "diverted-archives"
        diverted.mkdir()
        archive_root.symlink_to(diverted, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "exact reviewed path"):
            apply(
                journal,
                expected_plan_digest=str(plan["plan_digest"]),
                confirm_agents_stopped=True,
                confirm_no_legacy_writers=True,
                confirm_retire_active_authority=True,
            )
        self.assertTrue(legacy.exists())
        self.assertFalse((diverted / str(plan["cutover_id"])).exists())


class ObserverTest(GitWorkspaceTest):
    def test_schema_one_events_are_cataloged_without_legacy_authority(self) -> None:
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="observe")
        create_claim(self.root, scope="observe", owner="agent-a", run_id="run-a", task="observe", paths=["app.txt"])
        (self.root / ".agent-coordination").mkdir()
        (self.root / ".agent-coordination/legacy-event.json").write_text('{"event":"claim-created"}\n')
        # A writable legacy namespace deliberately makes producer resolution fail;
        # remove it to model the required tombstone/current state relationship.
        for child in (self.root / ".agent-coordination").iterdir():
            child.unlink()
        (self.root / ".agent-coordination").rmdir()

        broken = Path(self.temporary.name) / "broken"
        (broken / ".dev-mesh").mkdir(parents=True)
        (broken / ".dev-mesh/manifest.json").write_text("{}\n", encoding="utf-8")

        database = Path(self.temporary.name) / "observer.sqlite3"
        with Catalog(database) as catalog:
            collected = catalog.collect_workspace(self.root)
            scanned = catalog.collect_roots([Path(self.temporary.name)])
            report = catalog.report(workspace=workspace_id(self.root))
        self.assertGreaterEqual(collected["inserted_events"], 2)
        self.assertEqual(report["protocol_version"], "20260814.1")
        self.assertEqual(report["active"]["claim"], 1)
        self.assertEqual(report["event_counts"]["claim-created"], 1)
        self.assertEqual(report["non_collaborative_runs"], [])
        self.assertEqual(len(scanned["discovery_issues"]), 1)
        self.assertEqual(discover_workspaces([Path(self.temporary.name)]), [self.root.resolve()])
