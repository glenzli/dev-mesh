from __future__ import annotations

import json
from unittest import mock

from dev_mesh_coord import canonical_git, work_results
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import (
    create_claim,
    join_run,
    leave_run,
    release_claim,
    recover_run_authority,
)

from helpers import GitWorkspaceTest, git


class WorkResultTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="shared edit")
        create_claim(
            self.root,
            scope="shared-edit",
            owner="agent-a",
            run_id="run-a",
            task="edit app",
            paths=["app.txt"],
            validation="focused checks",
        )

    def _complete(self, result_id: str = "result-a") -> dict[str, object]:
        return work_results.complete_claim(
            self.root,
            result_id=result_id,
            scope="shared-edit",
            owner="agent-a",
            run_id="run-a",
            summary="app work complete",
            validation_evidence="focused checks passed",
        )

    def test_dirty_work_completes_without_commit_or_live_authority(self) -> None:
        base = git(self.root, "rev-parse", "HEAD")
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        result = self._complete()
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.root, "diff", "--name-only"), "app.txt")
        plane = resolve(self.root)
        self.assertFalse(plane.state_root.joinpath("claims/shared-edit.json").exists())
        self.assertTrue(plane.state_root.joinpath("work-results/result-a.json").is_file())
        closed = leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="result recorded",
        )
        self.assertEqual(closed["status"], "closed")

    def test_next_writer_must_accept_exact_dirty_baseline(self) -> None:
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        self._complete()
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue shared edit")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        self.assertEqual(claim["status"], "pending-baseline")
        baseline = claim["baseline"]
        self.assertEqual(baseline["related_result_ids"], ["result-a"])
        accepted = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=str(baseline["baseline_sha256"]),
        )
        self.assertEqual(accepted["status"], "active")

    def test_changed_baseline_refreshes_without_granting_authority(self) -> None:
        (self.root / "app.txt").write_text("base\nfirst\n", encoding="utf-8")
        self._complete()
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        digest = str(claim["baseline"]["baseline_sha256"])
        (self.root / "app.txt").write_text("base\nchanged again\n", encoding="utf-8")
        refreshed = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=digest,
        )
        self.assertEqual(refreshed["status"], "pending-baseline")
        self.assertTrue(refreshed["baseline_changed"])
        self.assertFalse(refreshed["baseline_accepted"])
        new_digest = str(refreshed["baseline"]["baseline_sha256"])
        self.assertNotEqual(new_digest, digest)
        uncertain_retry = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=digest,
        )
        self.assertEqual(uncertain_retry["status"], "pending-baseline")
        self.assertEqual(
            uncertain_retry["baseline"]["baseline_sha256"], new_digest
        )
        self.assertFalse(uncertain_retry["baseline_accepted"])
        accepted = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=new_digest,
        )
        self.assertEqual(accepted["status"], "active")
        self.assertTrue(accepted["baseline_accepted"])


    def test_same_content_on_new_revision_requires_fresh_acceptance(self) -> None:
        (self.root / "app.txt").write_text("base\ninherited\n", encoding="utf-8")
        self._complete()
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        digest = str(claim["baseline"]["baseline_sha256"])
        old_revision = str(claim["baseline"]["observed_revision"])
        (self.root / "other.txt").write_text("new canonical fact\n", encoding="utf-8")
        git(self.root, "add", "other.txt")
        git(self.root, "commit", "-m", "advance unrelated canonical state")

        refreshed = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=digest,
        )
        self.assertEqual(refreshed["status"], "pending-baseline")
        self.assertTrue(refreshed["baseline_changed"])
        self.assertEqual(refreshed["baseline"]["baseline_sha256"], digest)
        self.assertNotEqual(refreshed["baseline"]["observed_revision"], old_revision)

        accepted = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=digest,
        )
        self.assertEqual(accepted["status"], "active")
        self.assertTrue(accepted["baseline_accepted"])

    def test_baseline_activation_event_gap_retries_once(self) -> None:
        (self.root / "app.txt").write_text("base\ninherited\n", encoding="utf-8")
        self._complete()
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        digest = str(claim["baseline"]["baseline_sha256"])
        with mock.patch.object(work_results, "_ensure_event", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                work_results.accept_baseline(
                    self.root,
                    scope="continued-edit",
                    owner="agent-b",
                    run_id="run-b",
                    baseline_sha256=digest,
                )
        retained = json.loads(
            resolve(self.root).state_root.joinpath("claims/continued-edit.json").read_text()
        )
        self.assertEqual(retained["status"], "pending-baseline")
        self.assertIn("baseline_activation", retained)

        retried = work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=digest,
        )
        self.assertEqual(retried["status"], "active")
        events = [
            json.loads(path.read_text())
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sum(event["event"] == "claim-baseline-accepted" for event in events), 1
        )

    def test_recovery_finishes_baseline_activation_then_rebinds_claim(self) -> None:
        (self.root / "app.txt").write_text("base\ninherited\n", encoding="utf-8")
        self._complete()
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        digest = str(claim["baseline"]["baseline_sha256"])
        with mock.patch.object(work_results, "_ensure_event", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                work_results.accept_baseline(
                    self.root,
                    scope="continued-edit",
                    owner="agent-b",
                    run_id="run-b",
                    baseline_sha256=digest,
                )
        leave_run(
            self.root,
            run_id="run-b",
            owner="agent-b",
            outcome="failed",
            summary="baseline caller stopped",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-c", owner="agent-b", task="recover baseline")
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-b",
            owner="agent-b",
            recovery_run_id="run-c",
            evidence="Finish the sealed baseline grant and continue.",
        )
        self.assertEqual(recovered["authority_recovered_to"], "run-c")
        current = json.loads(
            resolve(self.root).state_root.joinpath("claims/continued-edit.json").read_text()
        )
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["run_id"], "run-c")
        self.assertNotIn("baseline_activation", current)

    def test_completion_event_gap_retries_with_one_result_and_event(self) -> None:
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        with mock.patch.object(work_results, "_ensure_event", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self._complete()
        claim = json.loads(resolve(self.root).state_root.joinpath("claims/shared-edit.json").read_text())
        self.assertEqual(claim["status"], "completing")
        retried = self._complete()
        self.assertEqual(retried["result_id"], "result-a")
        events = [
            json.loads(path.read_text())
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(sum(event["event"] == "claim-completed" for event in events), 1)

    def test_closed_run_recovery_finishes_sealed_completion_without_rebinding_it(self) -> None:
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        with mock.patch.object(work_results, "_ensure_event", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self._complete()
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="failed",
            summary="completion caller stopped",
            force_terminal=True,
            reason_code="process-lost",
        )
        join_run(self.root, run_id="run-recovery", owner="agent-a", task="recover completion")
        recovered = recover_run_authority(
            self.root,
            closed_run_id="run-a",
            owner="agent-a",
            recovery_run_id="run-recovery",
            evidence="Finish the exact sealed completion intent.",
        )
        self.assertEqual(
            recovered["authority_recovered_to"], "run-recovery"
        )
        plane = resolve(self.root)
        self.assertFalse(plane.state_root.joinpath("claims/shared-edit.json").exists())
        result = json.loads(
            plane.state_root.joinpath("work-results/result-a.json").read_text()
        )
        self.assertEqual(result["run_id"], "run-a")
        events = [
            json.loads(path.read_text())
            for path in plane.state_root.joinpath("events").glob("*.json")
        ]
        completed = [event for event in events if event["event"] == "claim-completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["run_id"], "run-a")

    def test_independent_publisher_consumes_result_without_original_claim(self) -> None:
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        self._complete()
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="implementation complete",
        )
        join_run(self.root, run_id="run-p", owner="publisher", task="publish results")
        published = canonical_git.commit_results(
            self.root,
            result_ids=["result-a"],
            owner="publisher",
            run_id="run-p",
            summary="publish completed app work",
            validation_evidence="result validation reviewed",
        )
        self.assertEqual(published["status"], "completed")
        self.assertEqual(git(self.root, "show", "HEAD:app.txt"), "base\ncompleted dirty")
        self.assertEqual(published["work_result_ids"], ["result-a"])

    def test_independent_publication_waits_for_later_overlapping_editor(self) -> None:
        (self.root / "app.txt").write_text("base\ncompleted dirty\n", encoding="utf-8")
        self._complete()
        leave_run(
            self.root,
            run_id="run-a",
            owner="agent-a",
            outcome="completed",
            summary="implementation complete",
        )
        join_run(self.root, run_id="run-b", owner="agent-b", task="continue editing")
        claim = create_claim(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            task="continue app",
            paths=["app.txt"],
        )
        work_results.accept_baseline(
            self.root,
            scope="continued-edit",
            owner="agent-b",
            run_id="run-b",
            baseline_sha256=str(claim["baseline"]["baseline_sha256"]),
        )
        join_run(self.root, run_id="run-p", owner="publisher", task="publish results")
        head = git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "active editing authority"):
            canonical_git.commit_results(
                self.root,
                result_ids=["result-a"],
                owner="publisher",
                run_id="run-p",
                summary="publish completed app work",
                validation_evidence="result validation reviewed",
            )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)


class WorkspaceBytesWorkResultTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "ignore local data")
        (self.root / "local").mkdir()
        (self.root / "local/state.json").write_text(
            '{"owner":"before"}\n', encoding="utf-8"
        )
        initialize(self.root)
        join_run(
            self.root,
            run_id="run-data-a",
            owner="agent-a",
            task="update local data",
        )

    def _claim_existing(self) -> dict[str, object]:
        return create_claim(
            self.root,
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            task="update ignored local data",
            paths=["local/state.json"],
            projection_mode="workspace-bytes",
        )

    def test_ignored_file_completes_and_next_writer_accepts_exact_bytes(self) -> None:
        claim = self._claim_existing()
        self.assertEqual(claim["status"], "pending-baseline")
        self.assertEqual(claim["baseline"]["projection_mode"], "workspace-bytes")
        accepted = work_results.accept_baseline(
            self.root,
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            baseline_sha256=str(claim["baseline"]["baseline_sha256"]),
        )
        self.assertEqual(accepted["status"], "active")
        (self.root / "local/state.json").write_text(
            '{"owner":"agent-a"}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "complete the Claim"):
            release_claim(
                self.root,
                scope="local-data",
                owner="agent-a",
                run_id="run-data-a",
                summary="must retain evidence",
            )
        result = work_results.complete_claim(
            self.root,
            result_id="local-data-result",
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            summary="updated ignored local data",
            validation_evidence="JSON decoded successfully",
        )
        self.assertEqual(result["projection_mode"], "workspace-bytes")
        self.assertEqual(result["actual_paths"], ["local/state.json"])
        self.assertNotIn("result_tree", result)

        join_run(
            self.root,
            run_id="run-data-b",
            owner="agent-b",
            task="continue local data",
        )
        continued = create_claim(
            self.root,
            scope="continued-local-data",
            owner="agent-b",
            run_id="run-data-b",
            task="continue ignored local data",
            paths=["local/state.json"],
            projection_mode="workspace-bytes",
        )
        self.assertEqual(continued["status"], "pending-baseline")
        self.assertEqual(
            continued["baseline"]["related_result_ids"], ["local-data-result"]
        )
        accepted_next = work_results.accept_baseline(
            self.root,
            scope="continued-local-data",
            owner="agent-b",
            run_id="run-data-b",
            baseline_sha256=str(continued["baseline"]["baseline_sha256"]),
        )
        self.assertEqual(accepted_next["status"], "active")
        released = release_claim(
            self.root,
            scope="continued-local-data",
            owner="agent-b",
            run_id="run-data-b",
            summary="inspected without changes",
        )
        self.assertEqual(released["status"], "released")

    def test_missing_ignored_file_can_be_claimed_before_creation(self) -> None:
        claim = create_claim(
            self.root,
            scope="future-local-data",
            owner="agent-a",
            run_id="run-data-a",
            task="create ignored local data",
            paths=["local/future.json"],
            projection_mode="workspace-bytes",
        )
        self.assertEqual(claim["status"], "active")
        (self.root / "local/future.json").write_text(
            '{"created":true}\n', encoding="utf-8"
        )
        result = work_results.complete_claim(
            self.root,
            result_id="future-local-result",
            scope="future-local-data",
            owner="agent-a",
            run_id="run-data-a",
            summary="created ignored file",
            validation_evidence="JSON decoded successfully",
        )
        self.assertEqual(result["actual_paths"], ["local/future.json"])

    def test_unrelated_git_revision_does_not_invalidate_workspace_bytes_baseline(self) -> None:
        claim = self._claim_existing()
        digest = str(claim["baseline"]["baseline_sha256"])
        (self.root / "other.txt").write_text("unrelated canonical change\n", encoding="utf-8")
        git(self.root, "add", "other.txt")
        git(self.root, "commit", "-m", "advance unrelated source")
        accepted = work_results.accept_baseline(
            self.root,
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            baseline_sha256=digest,
        )
        self.assertEqual(accepted["status"], "active")
        self.assertTrue(accepted["baseline_accepted"])

    def test_workspace_bytes_result_and_claim_cannot_publish_through_git(self) -> None:
        claim = self._claim_existing()
        work_results.accept_baseline(
            self.root,
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            baseline_sha256=str(claim["baseline"]["baseline_sha256"]),
        )
        (self.root / "local/state.json").write_text(
            '{"owner":"agent-a"}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "cannot publish through Git"):
            canonical_git.commit(
                self.root,
                scope="local-data",
                owner="agent-a",
                run_id="run-data-a",
                summary="must not publish ignored bytes",
                validation_evidence="local validation",
            )
        work_results.complete_claim(
            self.root,
            result_id="local-data-result",
            scope="local-data",
            owner="agent-a",
            run_id="run-data-a",
            summary="updated ignored local data",
            validation_evidence="local validation",
        )
        with self.assertRaisesRegex(ValueError, "cannot publish through Git"):
            canonical_git.commit_results(
                self.root,
                result_ids=["local-data-result"],
                owner="agent-a",
                run_id="run-data-a",
                summary="must not publish ignored bytes",
                validation_evidence="local validation",
            )
