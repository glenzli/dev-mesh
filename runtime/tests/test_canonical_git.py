from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from unittest import mock

from dev_mesh_coord import canonical_git, contention, git_backend, git_effects, transactions
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import create_claim, join_run

from helpers import GitWorkspaceTest, git


class CanonicalGitTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="direct work")
        join_run(self.root, run_id="run-s", owner="steward", task="reconcile")
        create_claim(
            self.root,
            scope="direct",
            owner="agent-a",
            run_id="run-a",
            task="edit app",
            paths=["app.txt"],
            semantic_writes=["direct-slice"],
            validation="focused direct checks",
        )

    def _commit(self, summary: str = "direct candidate") -> dict[str, object]:
        return canonical_git.commit(
            self.root,
            scope="direct",
            owner="agent-a",
            run_id="run-a",
            summary=summary,
            validation_evidence="focused direct validation passed",
        )

    def test_direct_commit_preserves_unrelated_unstaged_dirty_and_archives_intent(self) -> None:
        (self.root / "app.txt").write_text("base\ndirect\n", encoding="utf-8")
        (self.root / "other.txt").write_text("unrelated dirty\n", encoding="utf-8")
        completed = self._commit()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            git(self.root, "show", "HEAD:app.txt"), "base\ndirect"
        )
        self.assertEqual(git(self.root, "show", "HEAD:other.txt"), "other")
        self.assertEqual(
            (self.root / "other.txt").read_text(encoding="utf-8"),
            "unrelated dirty\n",
        )
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(
            git(self.root, "diff", "--name-only"), "other.txt"
        )
        direct_commit_id = str(completed["direct_commit_id"])
        plane = resolve(self.root)
        self.assertFalse(
            plane.state_root.joinpath(
                f"direct-commits/active/{direct_commit_id}.json"
            ).exists()
        )
        self.assertTrue(
            plane.state_root.joinpath(
                f"direct-commits/archive/{direct_commit_id}.json"
            ).is_file()
        )
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in plane.state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sorted(
                event["event"]
                for event in events
                if event.get("direct_commit_id") == direct_commit_id
            ),
            ["direct-commit-completed", "direct-commit-started"],
        )

    def test_two_direct_commits_serialize_and_leave_no_index_state(self) -> None:
        (self.root / "app.txt").write_text("base\none\n", encoding="utf-8")
        first = self._commit("first direct")
        (self.root / "app.txt").write_text("base\none\ntwo\n", encoding="utf-8")
        second = self._commit("second direct")
        self.assertNotEqual(first["candidate_revision"], second["candidate_revision"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), second["candidate_revision"])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(canonical_git.doctor(self.root)["active_direct_commits"], [])

    def test_two_agents_concurrently_publish_disjoint_direct_claims(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="other direct work")
        create_claim(
            self.root,
            scope="other-direct",
            owner="agent-b",
            run_id="run-b",
            task="edit other",
            paths=["other.txt"],
            validation="focused other checks",
        )
        (self.root / "app.txt").write_text("base\nagent a\n", encoding="utf-8")
        (self.root / "other.txt").write_text("other\nagent b\n", encoding="utf-8")
        module_root = str(Path(canonical_git.__file__).resolve().parent.parent)
        child_code = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from dev_mesh_coord import canonical_git\n"
            "result = canonical_git.commit(Path(sys.argv[1]), scope=sys.argv[2], "
            "owner=sys.argv[3], run_id=sys.argv[4], summary=sys.argv[5], "
            "validation_evidence='concurrent direct validation passed')\n"
            "print(json.dumps(result))\n"
        )
        environment = {**os.environ, "PYTHONPATH": module_root}
        processes = [
            subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    child_code,
                    str(self.root),
                    scope,
                    owner,
                    run_id,
                    summary,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for scope, owner, run_id, summary in (
                ("direct", "agent-a", "run-a", "agent a direct"),
                ("other-direct", "agent-b", "run-b", "agent b direct"),
            )
        ]
        results: list[dict[str, object]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15.0)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))

        self.assertEqual([result["status"] for result in results], ["completed", "completed"])
        self.assertEqual(git(self.root, "show", "HEAD:app.txt"), "base\nagent a")
        self.assertEqual(git(self.root, "show", "HEAD:other.txt"), "other\nagent b")
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(canonical_git.doctor(self.root)["active_direct_commits"], [])

    def test_preexisting_index_and_out_of_scope_only_changes_are_rejected(self) -> None:
        (self.root / "app.txt").write_text("base\nstaged elsewhere\n", encoding="utf-8")
        git(self.root, "add", "app.txt")
        with self.assertRaisesRegex(ValueError, "index must be empty"):
            self._commit()
        git(self.root, "reset", "HEAD", "--", "app.txt")
        (self.root / "app.txt").write_text("base\n", encoding="utf-8")
        (self.root / "other.txt").write_text("outside only\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no declared-path changes"):
            self._commit()
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.root, "diff", "--name-only"), "other.txt")

    def test_git_permission_preflight_fails_before_durable_intent(self) -> None:
        (self.root / "app.txt").write_text("base\nblocked\n", encoding="utf-8")
        with mock.patch.object(
            canonical_git.git,
            "assert_canonical_git_writable",
            side_effect=PermissionError("canonical Git metadata is not writable"),
        ):
            with self.assertRaisesRegex(PermissionError, "not writable"):
                self._commit()

        self.assertEqual(canonical_git.doctor(self.root)["active_direct_commits"], [])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertFalse(
            any(str(event.get("event", "")).startswith("direct-commit-") for event in events)
        )

    def test_git_failure_keeps_stderr_tail_without_declared_path_noise(self) -> None:
        error = git_backend.GitCommandError(
            ("add", *(f"very-long-path-{index}" for index in range(200))),
            128,
            "fatal: Unable to create '.git/index.lock': Operation not permitted",
        )
        self.assertIn("Git add failed with exit 128", str(error))
        self.assertIn("Operation not permitted", str(error))
        self.assertNotIn("very-long-path", str(error))

    def test_unresolved_direct_intent_blocks_transaction_publish(self) -> None:
        (self.root / "app.txt").write_text("base\ndirect\n", encoding="utf-8")
        original = canonical_git._advance

        def stop_after_intent(*args: object, **kwargs: object):
            raise RuntimeError("injected durable direct intent")

        canonical_git._advance = stop_after_intent
        try:
            attention = self._commit()
        finally:
            canonical_git._advance = original
        self.assertEqual(attention["status"], "needs-attention")

        # The exact publication internals are covered by transaction tests; this
        # predicate is the shared boundary both publish and reconcile call.
        with self.assertRaisesRegex(ValueError, "unresolved direct commit"):
            canonical_git.require_publish_allowed(resolve(self.root))
        reconciled = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        self.assertEqual(len(reconciled["completed"]), 1)

    def test_actual_transaction_publish_rejects_unresolved_direct_intent(self) -> None:
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel")
        pending = create_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            task="parallel app edit",
            paths=["app.txt"],
            semantic_writes=["parallel-slice"],
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="parallel-tx",
            reason="exercise canonical publish boundary",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (
            ("direct", "agent-a", "run-a"),
            ("parallel", "agent-b", "run-b"),
        ):
            contention.respond(
                self.root,
                contention_id=contention_id,
                scope=scope,
                owner=owner,
                run_id=run_id,
                revision=revision,
                accept=True,
            )
        contention.enact(
            self.root,
            contention_id=contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
        )
        (self.root / "app.txt").write_text("base\ndirect intent\n", encoding="utf-8")
        original = canonical_git._advance
        canonical_git._advance = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("retain direct intent")
        )
        try:
            direct = self._commit("retained direct intent")
        finally:
            canonical_git._advance = original
        self.assertEqual(direct["status"], "needs-attention")

        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=contention_id,
            reason="parallel candidate",
        )
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\nparallel\n", encoding="utf-8")
        transactions.prepare(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            summary="parallel candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            evidence="parallel candidate exact",
        )
        with self.assertRaisesRegex(ValueError, "unresolved direct commit"):
            transactions.publish(
                self.root,
                transaction_id=str(started["transaction_id"]),
                steward="agent-b",
                steward_run_id="run-b",
            )

    def test_reconcile_rejects_same_base_on_another_symbolic_branch(self) -> None:
        (self.root / "app.txt").write_text("base\nbranch bound\n", encoding="utf-8")
        original = canonical_git._advance
        canonical_git._advance = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("retain branch-bound intent")
        )
        try:
            retained = self._commit("branch-bound direct")
        finally:
            canonical_git._advance = original
        self.assertEqual(retained["status"], "needs-attention")
        main_before = git(self.root, "rev-parse", "refs/heads/main")
        git(self.root, "switch", "-c", "other")

        result = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        self.assertEqual(result["completed"], [])
        self.assertIn("branch changed", str(result["attention"][0]["error"]))
        self.assertEqual(git(self.root, "rev-parse", "refs/heads/main"), main_before)
        self.assertEqual(git(self.root, "rev-parse", "refs/heads/other"), main_before)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")

    def test_reconcile_never_stages_content_changed_after_durable_intent(self) -> None:
        (self.root / "app.txt").write_text("base\nreviewed\n", encoding="utf-8")
        original = canonical_git._advance
        canonical_git._advance = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("retain exact content intent")
        )
        try:
            retained = self._commit("content-bound direct")
        finally:
            canonical_git._advance = original
        self.assertEqual(retained["status"], "needs-attention")
        (self.root / "app.txt").write_text(
            "base\nreviewed\nchanged later\n", encoding="utf-8"
        )

        result = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        self.assertEqual(result["completed"], [])
        self.assertIn("content changed", str(result["attention"][0]["error"]))
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.root, "show", "HEAD:app.txt"), "base")

    def test_reconcile_completes_update_ref_effect_ahead_once(self) -> None:
        (self.root / "app.txt").write_text("base\neffect ahead\n", encoding="utf-8")
        original = canonical_git._run_git

        def stop_after_update_ref(
            root: Path, *arguments: str, **kwargs: object
        ):
            result = original(root, *arguments, **kwargs)
            if arguments[:1] == ("update-ref",):
                raise RuntimeError("injected stop after update-ref")
            return result

        with mock.patch.object(
            canonical_git, "_run_git", side_effect=stop_after_update_ref
        ):
            retained = self._commit("effect-ahead direct")
        self.assertEqual(retained["status"], "needs-attention")
        candidate = str(retained["candidate_revision"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), candidate)

        result = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        self.assertEqual(len(result["completed"]), 1)
        repeated = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        self.assertEqual(repeated["completed"], [])
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in resolve(self.root).state_root.joinpath("events").glob("*.json")
        ]
        self.assertEqual(
            sum(
                event.get("event") == "direct-commit-completed"
                and event.get("direct_commit_id") == retained["direct_commit_id"]
                for event in events
            ),
            1,
        )

    def test_terminal_event_gap_reuses_one_durable_event_id(self) -> None:
        (self.root / "app.txt").write_text("base\nevent gap\n", encoding="utf-8")
        original = canonical_git.write_event
        failed = False

        def fail_terminal_once(plane: object, event: dict[str, object]):
            nonlocal failed
            if not failed and event.get("event") == "direct-commit-completed":
                failed = True
                raise RuntimeError("injected terminal event gap")
            return original(plane, event)

        with mock.patch.object(
            canonical_git, "write_event", side_effect=fail_terminal_once
        ):
            retained = self._commit("event-gap direct")
        self.assertEqual(retained["status"], "needs-attention")
        terminal_id = str(retained["terminal_event"]["event_id"])

        completed = canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )["completed"][0]
        self.assertEqual(completed["terminal_event"]["event_id"], terminal_id)
        canonical_git.reconcile(
            self.root, steward="steward", steward_run_id="run-s"
        )
        matching = list(
            resolve(self.root).state_root.joinpath("events").glob(
                f"*-{terminal_id}-direct-commit-completed.json"
            )
        )
        self.assertEqual(len(matching), 1)

    def test_event_recovery_uses_only_exact_event_id_globs(self) -> None:
        (self.root / "app.txt").write_text("base\nexact events\n", encoding="utf-8")
        patterns: list[str] = []
        original = canonical_git._matching_event_paths

        def inspect_pattern(plane: object, event: dict[str, object]):
            event_id = str(event["event_id"])
            event_name = str(event["event"])
            patterns.append(f"*-{event_id}-{event_name}.json")
            return original(plane, event)

        with mock.patch.object(
            canonical_git, "_matching_event_paths", side_effect=inspect_pattern
        ):
            self._commit("exact-event lookup")
        self.assertEqual(len(patterns), 2)
        self.assertTrue(all(pattern != "*.json" for pattern in patterns))
        self.assertTrue(all("direct-commit-" in pattern for pattern in patterns))

    def test_real_sigkill_leaves_delayed_stage_child_holding_canonical_fence(self) -> None:
        if os.name != "posix":
            self.skipTest("inherited-FD Git fencing requires POSIX")
        real_git = shutil.which("git")
        if real_git is None:
            self.skipTest("Git is unavailable")
        (self.root / "app.txt").write_text("base\ndelayed stage\n", encoding="utf-8")
        wrapper_root = Path(self.temporary.name) / "direct-git-wrapper"
        wrapper_root.mkdir()
        entered = wrapper_root / "entered"
        release = wrapper_root / "release"
        completed = wrapper_root / "completed"
        wrapper_pid = wrapper_root / "pid"
        wrapper = wrapper_root / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$3:$4\" = \"add:-A\" ] && [ -z \"$GIT_INDEX_FILE\" ]; then\n"
            f"  echo $$ > {shlex.quote(str(wrapper_pid))}\n"
            f"  : > {shlex.quote(str(entered))}\n"
            "  attempts=0\n"
            f"  while [ ! -f {shlex.quote(str(release))} ]; do\n"
            "    attempts=$((attempts + 1))\n"
            "    if [ \"$attempts\" -gt 300 ]; then exit 98; fi\n"
            "    sleep 0.05\n"
            "  done\n"
            f"  {shlex.quote(real_git)} \"$@\"\n"
            "  result=$?\n"
            f"  : > {shlex.quote(str(completed))}\n"
            "  exit $result\n"
            "fi\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        child_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from dev_mesh_coord import canonical_git\n"
            "canonical_git.commit(Path(sys.argv[1]), scope='direct', owner='agent-a', "
            "run_id='run-a', summary='delayed direct', validation_evidence='exact')\n"
        )
        module_root = str(Path(canonical_git.__file__).resolve().parent.parent)
        publisher_log = wrapper_root / "publisher.log"
        stream = publisher_log.open("wb")
        publisher = subprocess.Popen(
            (sys.executable, "-c", child_code, str(self.root)),
            env={
                **os.environ,
                "PATH": str(wrapper_root) + os.pathsep + os.environ.get("PATH", ""),
                "PYTHONPATH": module_root,
            },
            stdout=stream,
            stderr=subprocess.STDOUT,
        )

        def wait_for(path: Path, timeout: float = 15.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if path.exists():
                    return True
                time.sleep(0.01)
            return path.exists()

        try:
            self.assertTrue(
                wait_for(entered),
                f"stage child did not start; rc={publisher.poll()} "
                f"log={publisher_log.read_text(encoding='utf-8', errors='replace')}",
            )
            plane = resolve(self.root)
            self.assertTrue(git_effects.is_canonical_in_flight(plane))
            publisher.kill()
            self.assertEqual(publisher.wait(timeout=5.0), -signal.SIGKILL)
            in_flight = canonical_git.reconcile(
                self.root, steward="steward", steward_run_id="run-s"
            )
            self.assertEqual(len(in_flight["in_flight"]), 1)
            release.write_text("continue\n", encoding="utf-8")
            self.assertTrue(wait_for(completed), "delayed stage child did not finish")
            deadline = time.monotonic() + 15.0
            while git_effects.is_canonical_in_flight(plane) and time.monotonic() < deadline:
                time.sleep(0.01)
            result = canonical_git.reconcile(
                self.root, steward="steward", steward_run_id="run-s"
            )
            self.assertEqual(len(result["completed"]), 1)
            self.assertEqual(git(self.root, "show", "HEAD:app.txt"), "base\ndelayed stage")
        finally:
            release.touch(exist_ok=True)
            if publisher.poll() is None:
                publisher.kill()
                publisher.wait(timeout=5.0)
            if not completed.exists() and wrapper_pid.exists():
                try:
                    os.kill(int(wrapper_pid.read_text(encoding="utf-8")), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
            stream.close()
