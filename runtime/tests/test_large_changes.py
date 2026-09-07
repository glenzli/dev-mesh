from __future__ import annotations

import json
from pathlib import Path

from dev_mesh_coord import canonical_git, contention, transactions, work_results
from dev_mesh_coord.constants import MAX_EVENT_BYTES
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import create_claim, join_run
from dev_mesh_coord.workspace_projection import declared_projection
from helpers import GitWorkspaceTest, git


class LargeChangesTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="large changes")

    def _claim(self) -> dict[str, object]:
        return create_claim(
            self.root, scope="bulk", owner="agent-a", run_id="run-a",
            task="generated output", paths=["generated"], validation="exact tree checks",
            semantic_writes=["primary-output"],
        )

    def _files(self, root: Path, count: int, *, long_paths: bool = False) -> list[str]:
        directory = root / "generated"
        if long_paths:
            directory = directory / ("a" * 100) / ("b" * 100) / ("c" * 100)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(count):
            # Literal pathspec handling must preserve brackets, spaces and newlines.
            path = directory / f"page [{index:04d}]\noutput.txt"
            path.write_text(f"page {index}\n")
            paths.append(path.relative_to(root).as_posix())
        return sorted(paths)

    def _assert_published(self, result: dict[str, object], paths: list[str], tree: str) -> None:
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["staged_paths"], paths)
        self.assertEqual(git(self.root, "rev-parse", "HEAD^{tree}"), tree)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.root, "diff", "--name-only"), "other.txt")
        self.assertEqual(git(self.root, "show", "HEAD:other.txt"), "other")
        self.assertEqual((self.root / "other.txt").read_text(), "unrelated dirty\n")
        projected_events = []
        for path in (resolve(self.root).state_root / "events").glob("*.json"):
            self.assertLessEqual(path.stat().st_size, MAX_EVENT_BYTES)
            event = json.loads(path.read_text())
            if "actual_path_count" in event:
                projected_events.append(event)
                self.assertLessEqual(len(event["actual_path_sample"]), 16)
                self.assertNotIn("actual_paths", event)
        self.assertTrue(any(event["actual_path_count"] == len(paths) for event in projected_events))

    def test_direct_commit_1093_long_paths_without_argv_limit(self) -> None:
        self._claim()
        paths = self._files(self.root, 1093, long_paths=True)
        self.assertGreater(sum(len(path.encode()) + 1 for path in paths), 262144)
        (self.root / "other.txt").write_text("unrelated dirty\n")
        projection = declared_projection(
            self.root, resolve(self.root), ["generated"], git(self.root, "rev-parse", "HEAD"),
            require_changes=True,
        )
        self.assertEqual(projection["actual_paths"], paths)
        result = canonical_git.commit(
            self.root, scope="bulk", owner="agent-a", run_id="run-a",
            summary="1093 generated files", validation_evidence="exact generated tree checked",
        )
        self._assert_published(result, paths, str(projection["expected_index_tree"]))

    def test_1093_inherited_files_drift_accept_complete_and_publish(self) -> None:
        paths = self._files(self.root, 1093)
        (self.root / "other.txt").write_text("unrelated dirty\n")
        claim = self._claim()
        self.assertEqual(claim["status"], "pending-baseline")
        baseline = claim["baseline"]
        self.assertEqual(baseline["actual_path_count"], 1093)
        (self.root / paths[0]).write_text("changed during review\n")
        refreshed = work_results.accept_baseline(
            self.root, scope="bulk", owner="agent-a", run_id="run-a",
            baseline_sha256=str(baseline["baseline_sha256"]),
        )
        self.assertFalse(refreshed["baseline_accepted"])
        accepted = work_results.accept_baseline(
            self.root, scope="bulk", owner="agent-a", run_id="run-a",
            baseline_sha256=str(refreshed["baseline"]["baseline_sha256"]),
        )
        self.assertEqual(accepted["status"], "active")
        (self.root / paths[-1]).write_text("reviewed and updated\n")
        result = work_results.complete_claim(
            self.root, result_id="bulk-result", scope="bulk", owner="agent-a", run_id="run-a",
            summary="generated output ready", validation_evidence="1093 files validated",
        )
        self.assertEqual(result["actual_path_count"], 1093)
        published = canonical_git.commit_results(
            self.root, result_ids=["bulk-result"], owner="agent-a", run_id="run-a",
            summary="publish generated output", validation_evidence="reviewed Work Result",
        )
        self._assert_published(published, paths, str(result["result_tree"]))

    def test_microtransaction_still_rejects_129_changed_files(self) -> None:
        self._claim()
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel change")
        pending = create_claim(
            self.root, scope="parallel", owner="agent-b", run_id="run-b",
            task="parallel output", paths=["generated"], allow_overlap=True,
            semantic_writes=["parallel-output"],
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root, contention_id=contention_id, owner="agent-b", run_id="run-b",
            epoch=1, decision="parallel-tx", reason="independent candidate",
        )
        for scope, owner, run_id in (("bulk", "agent-a", "run-a"), ("parallel", "agent-b", "run-b")):
            contention.respond(
                self.root, contention_id=contention_id, scope=scope, owner=owner,
                run_id=run_id, revision=int(proposed["decision_revision"]), accept=True,
            )
        contention.enact(self.root, contention_id=contention_id, owner="agent-b", run_id="run-b", epoch=1)
        started = transactions.begin(
            self.root, scope="parallel", owner="agent-b", run_id="run-b",
            contention_id=contention_id, reason="bounded candidate",
        )
        base = git(self.root, "rev-parse", "HEAD")
        self._files(Path(str(started["checkout"])), 129)
        with self.assertRaisesRegex(ValueError, "microtransaction changes more than 128"):
            transactions.prepare(
                self.root, transaction_id=str(started["transaction_id"]), owner="agent-b",
                owner_run_id="run-b", summary="oversized candidate",
            )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), base)
        self.assertFalse((self.root / "generated").exists())
