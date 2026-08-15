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

from dev_mesh_coord import canonical_git, contention, git_effects, transactions
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import create_claim, join_run, release_claim

from helpers import GitWorkspaceTest, git


class TransactionTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        initialize(self.root)
        join_run(self.root, run_id="run-a", owner="agent-a", task="primary")
        join_run(self.root, run_id="run-b", owner="agent-b", task="parallel")
        create_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            task="primary",
            paths=["app.txt"],
            semantic_writes=["left-side"],
        )
        pending = create_claim(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            task="parallel",
            paths=["app.txt"],
            semantic_writes=["right-side"],
            allow_overlap=True,
        )
        self.contention_id = str(pending["contention_id"])
        proposed = contention.propose(
            self.root,
            contention_id=self.contention_id,
            owner="agent-b",
            run_id="run-b",
            epoch=1,
            decision="parallel-tx",
            reason="one short independent change",
        )
        revision = int(proposed["decision_revision"])
        for scope, owner, run_id in (("primary", "agent-a", "run-a"), ("parallel", "agent-b", "run-b")):
            contention.respond(self.root, contention_id=self.contention_id, scope=scope, owner=owner, run_id=run_id, revision=revision, accept=True)
        contention.enact(self.root, contention_id=self.contention_id, owner="agent-b", run_id="run-b", epoch=1)

    def test_real_one_commit_checkout_validate_and_fast_forward_publish(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="fast branch",
        )
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\nparallel\n", encoding="utf-8")
        prepared = transactions.prepare(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            summary="parallel change",
        )
        transactions.validate(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            evidence="unit checks passed",
        )
        with self.assertRaisesRegex(ValueError, "active direct Claims"):
            transactions.publish(
                self.root,
                transaction_id=str(started["transaction_id"]),
                steward="agent-b",
                steward_run_id="run-b",
            )
        release_claim(self.root, scope="primary", owner="agent-a", run_id="run-a", summary="yield publication")
        published = transactions.publish(
            self.root,
            transaction_id=str(started["transaction_id"]),
            steward="agent-b",
            steward_run_id="run-b",
        )
        self.assertEqual(published["status"], "published")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), prepared["candidate_revision"])
        self.assertEqual((self.root / "app.txt").read_text(), "base\nparallel\n")
        self.assertFalse(checkout.exists())
        diagnosis = transactions.doctor(self.root)
        self.assertEqual(diagnosis["orphan_checkouts"], [])
        self.assertEqual(diagnosis["orphan_branches"], [])

    def test_publish_rejects_branch_changed_after_validation_before_canonical_mutation(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="validated branch must remain exact",
        )
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\nvalidated\n", encoding="utf-8")
        transactions.prepare(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            summary="validated candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            evidence="validated exact first candidate",
        )
        canonical_before = git(self.root, "rev-parse", "HEAD")
        (checkout / "app.txt").write_text("base\nvalidated\nunvalidated\n", encoding="utf-8")
        git(checkout, "add", "app.txt")
        git(checkout, "commit", "-m", "unvalidated descendant")
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="allow publication check",
        )
        with self.assertRaisesRegex(ValueError, "changed after validation"):
            transactions.publish(
                self.root,
                transaction_id=str(started["transaction_id"]),
                steward="agent-b",
                steward_run_id="run-b",
            )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), canonical_before)

    def test_publish_permission_preflight_retains_ready_transaction(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="permission preflight",
        )
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\npermission\n", encoding="utf-8")
        transactions.prepare(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            summary="permission candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            evidence="candidate validated",
        )
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="allow publication preflight",
        )
        canonical_before = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(
            transactions.git,
            "assert_canonical_git_writable",
            side_effect=PermissionError("canonical Git metadata is not writable"),
        ):
            with self.assertRaisesRegex(PermissionError, "not writable"):
                transactions.publish(
                    self.root,
                    transaction_id=str(started["transaction_id"]),
                    steward="agent-b",
                    steward_run_id="run-b",
                )

        record = json.loads(
            resolve(self.root).state_root.joinpath(
                "transactions", "active", f"{started['transaction_id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(record["status"], "ready")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), canonical_before)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")

    def test_prepare_requires_exact_caller_run_not_only_owner_slug(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="exact caller Run",
        )
        join_run(self.root, run_id="run-b-other", owner="agent-b", task="different session")
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\nparallel\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another exact owner Run"):
            transactions.prepare(
                self.root,
                transaction_id=str(started["transaction_id"]),
                owner="agent-b",
                owner_run_id="run-b-other",
                summary="must be fenced",
            )

    def test_abort_requires_explicit_discard(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="try isolated work",
        )
        with self.assertRaisesRegex(ValueError, "explicit discard"):
            transactions.abort(
                self.root,
                transaction_id=str(started["transaction_id"]),
                owner="agent-b",
                owner_run_id="run-b",
                reason_code="cancelled",
                reason="not useful",
                discard=False,
            )
        aborted = transactions.abort(
            self.root,
            transaction_id=str(started["transaction_id"]),
            owner="agent-b",
            owner_run_id="run-b",
            reason_code="cancelled",
            reason="not useful",
            discard=True,
        )
        self.assertEqual(aborted["status"], "aborted")
        claim = resolve(self.root).state_root / "claims/parallel.json"
        self.assertIn('"status": "pending-arbitration"', claim.read_text())

    def test_refresh_effect_recovers_as_prepared_and_requires_revalidation(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="refresh candidate after canonical advance",
        )
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\nparallel\n", encoding="utf-8")
        prepared = transactions.prepare(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            summary="parallel candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            evidence="validated before canonical advance",
        )
        (self.root / "other.txt").write_text("canonical advance\n", encoding="utf-8")
        git(self.root, "add", "other.txt")
        git(self.root, "commit", "-m", "advance canonical")

        refreshed = transactions.publish(
            self.root,
            transaction_id=transaction_id,
            steward="agent-b",
            steward_run_id="run-b",
        )
        self.assertEqual(refreshed["status"], "prepared")
        self.assertNotEqual(refreshed["candidate_revision"], prepared["candidate_revision"])
        self.assertNotIn("validation", refreshed)
        with self.assertRaisesRegex(ValueError, "ready before publication"):
            transactions.publish(
                self.root,
                transaction_id=transaction_id,
                steward="agent-b",
                steward_run_id="run-b",
            )

        transactions.validate(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            evidence="validated exact refreshed candidate",
        )
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="allow refreshed publication",
        )
        published = transactions.publish(
            self.root,
            transaction_id=transaction_id,
            steward="agent-b",
            steward_run_id="run-b",
        )
        self.assertEqual(published["status"], "published")

    def test_reconcile_finalizes_merge_effect_ahead_of_publishing_record(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="recover effect-ahead publication",
        )
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\neffect ahead\n", encoding="utf-8")
        prepared = transactions.prepare(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            summary="effect-ahead candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            evidence="exact candidate",
        )
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="allow publication",
        )
        original_run = git_effects.run

        def fail_after_merge(*args: object, **kwargs: object):
            result = original_run(*args, **kwargs)
            if args[4:6] == ("merge", "--ff-only"):
                raise RuntimeError("injected stop after merge effect")
            return result

        with mock.patch.object(git_effects, "run", side_effect=fail_after_merge):
            with self.assertRaisesRegex(RuntimeError, "after merge effect"):
                transactions.publish(
                    self.root,
                    transaction_id=transaction_id,
                    steward="agent-b",
                    steward_run_id="run-b",
                )
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), prepared["candidate_revision"])
        active = json.loads(
            resolve(self.root)
            .state_root.joinpath(f"transactions/active/{transaction_id}.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(active["status"], "publishing")

        reconciled = transactions.reconcile(
            self.root,
            steward="agent-a",
            steward_run_id="run-a",
        )
        self.assertEqual(reconciled["publications"][0]["action"], "publication-completed")
        self.assertFalse(
            resolve(self.root)
            .state_root.joinpath(f"transactions/active/{transaction_id}.json")
            .exists()
        )

    def test_sigkill_publisher_leaves_delayed_git_child_holding_effect_fence(self) -> None:
        if os.name != "posix":
            self.skipTest("inherited-FD Git fencing requires POSIX")
        real_git = shutil.which("git")
        if real_git is None:
            self.skipTest("Git is unavailable")
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="real inherited-FD publication fence",
        )
        transaction_id = str(started["transaction_id"])
        checkout = Path(str(started["checkout"]))
        (checkout / "app.txt").write_text("base\ndelayed child\n", encoding="utf-8")
        prepared = transactions.prepare(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            summary="delayed child candidate",
        )
        transactions.validate(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            evidence="exact delayed child candidate",
        )
        release_claim(
            self.root,
            scope="primary",
            owner="agent-a",
            run_id="run-a",
            summary="allow delayed publication",
        )

        wrapper_root = Path(self.temporary.name) / "git-wrapper"
        wrapper_root.mkdir()
        wrapper = wrapper_root / "git"
        entered = wrapper_root / "entered"
        release = wrapper_root / "release"
        completed = wrapper_root / "completed"
        wrapper_pid = wrapper_root / "pid"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$3:$4\" = \"merge:--ff-only\" ]; then\n"
            f"  echo $$ > {shlex.quote(str(wrapper_pid))}\n"
            f"  : > {shlex.quote(str(entered))}\n"
            "  attempts=0\n"
            f"  while [ ! -f {shlex.quote(str(release))} ]; do\n"
            "    attempts=$((attempts + 1))\n"
            "    if [ \"$attempts\" -gt 200 ]; then exit 98; fi\n"
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
            "from dev_mesh_coord import transactions\n"
            "transactions.publish(Path(sys.argv[1]), transaction_id=sys.argv[2], "
            "steward='agent-b', steward_run_id='run-b')\n"
        )
        module_root = str(Path(transactions.__file__).resolve().parent.parent)
        inherited_pythonpath = os.environ.get("PYTHONPATH", "")
        child_environment = {
            **os.environ,
            "PATH": str(wrapper_root) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": module_root
            + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
        }
        publisher_log = wrapper_root / "publisher.log"
        publisher_stream = publisher_log.open("wb")
        try:
            publisher = subprocess.Popen(
                (sys.executable, "-c", child_code, str(self.root), transaction_id),
                env=child_environment,
                stdout=publisher_stream,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            publisher_stream.close()
            raise

        def wait_for(path: Path, timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if path.exists():
                    return True
                time.sleep(0.01)
            return path.exists()

        try:
            self.assertTrue(
                wait_for(entered, 15.0),
                "delayed Git child did not start; "
                f"publisher_rc={publisher.poll()} "
                f"log={publisher_log.read_text(encoding='utf-8', errors='replace')}",
            )
            self.assertTrue(
                git_effects.is_in_flight(resolve(self.root), transaction_id)
            )
            publisher.kill()
            self.assertEqual(publisher.wait(timeout=5.0), -signal.SIGKILL)
            with self.assertRaises(git_effects.GitEffectInFlight):
                transactions.abort(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    reason_code="publisher-killed",
                    reason="must not abort while delayed Git still owns the fence",
                    discard=True,
                )
            with self.assertRaises(git_effects.GitEffectInFlight):
                canonical_git.commit(
                    self.root,
                    scope="primary",
                    owner="agent-a",
                    run_id="run-a",
                    summary="must wait for publication",
                    validation_evidence="canonical fence probe",
                )
            release.write_text("continue\n", encoding="utf-8")
            self.assertTrue(wait_for(completed, 15.0), "delayed Git child did not finish")
            deadline = time.monotonic() + 15.0
            while (
                git_effects.is_in_flight(resolve(self.root), transaction_id)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertFalse(
                git_effects.is_in_flight(resolve(self.root), transaction_id)
            )
            self.assertEqual(
                git(self.root, "rev-parse", "HEAD"), prepared["candidate_revision"]
            )
            reconciled = transactions.reconcile(
                self.root,
                steward="agent-a",
                steward_run_id="run-a",
            )
            self.assertEqual(
                reconciled["publications"][0]["action"], "publication-completed"
            )
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
            publisher_stream.close()

    def test_abort_handoff_and_reconcile_fail_closed_while_git_effect_is_in_flight(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="fence concurrent Git effect",
        )
        transaction_id = str(started["transaction_id"])
        join_run(self.root, run_id="run-c", owner="agent-c", task="receiver")
        with mock.patch.object(git_effects, "is_in_flight", return_value=True):
            with self.assertRaises(git_effects.GitEffectInFlight):
                transactions.abort(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    reason_code="in-flight",
                    reason="must not discard while Git is still running",
                    discard=True,
                )
            with self.assertRaises(git_effects.GitEffectInFlight):
                transactions.handoff(
                    self.root,
                    transaction_id=transaction_id,
                    owner="agent-b",
                    owner_run_id="run-b",
                    next_owner="agent-c",
                    next_run_id="run-c",
                    checkpoint="must wait for Git effect",
                )
            reconciled = transactions.reconcile(
                self.root,
                steward="agent-a",
                steward_run_id="run-a",
            )
        self.assertEqual(
            reconciled["in_flight"],
            [{"transaction_id": transaction_id, "status": "active"}],
        )

    def test_reconciled_refresh_attention_can_be_handed_off_and_discarded(self) -> None:
        started = transactions.begin(
            self.root,
            scope="parallel",
            owner="agent-b",
            run_id="run-b",
            contention_id=self.contention_id,
            reason="resolve refresh attention explicitly",
        )
        transaction_id = str(started["transaction_id"])
        plane = resolve(self.root)
        transaction_path = (
            plane.state_root / f"transactions/active/{transaction_id}.json"
        )
        record = json.loads(transaction_path.read_text(encoding="utf-8"))
        record["status"] = "refresh-needs-attention"
        transaction_path.write_text(json.dumps(record), encoding="utf-8")
        join_run(self.root, run_id="run-c", owner="agent-c", task="inspect attention")

        handed = transactions.handoff(
            self.root,
            transaction_id=transaction_id,
            owner="agent-b",
            owner_run_id="run-b",
            next_owner="agent-c",
            next_run_id="run-c",
            checkpoint="Git effect ended; receiver inspected the attention state",
        )
        self.assertEqual(handed["owner"], "agent-c")
        aborted = transactions.abort(
            self.root,
            transaction_id=transaction_id,
            owner="agent-c",
            owner_run_id="run-c",
            reason_code="attention-discarded",
            reason="Receiver explicitly discards the inspected checkout.",
            discard=True,
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertEqual(aborted["cleanup"]["status"], "completed")


class WorkspaceBytesTransactionTest(GitWorkspaceTest):
    def test_workspace_bytes_contention_rejects_parallel_transaction_decision(self) -> None:
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "ignore local data")
        initialize(self.root)
        join_run(self.root, run_id="run-data-a", owner="agent-a", task="first data writer")
        join_run(self.root, run_id="run-data-b", owner="agent-b", task="second data writer")
        create_claim(
            self.root,
            scope="data-primary",
            owner="agent-a",
            run_id="run-data-a",
            task="create ignored data",
            paths=["local/state.json"],
            semantic_writes=["local-state-a"],
            projection_mode="workspace-bytes",
        )
        pending = create_claim(
            self.root,
            scope="data-parallel",
            owner="agent-b",
            run_id="run-data-b",
            task="also create ignored data",
            paths=["local/state.json"],
            semantic_writes=["local-state-b"],
            projection_mode="workspace-bytes",
            allow_overlap=True,
        )
        contention_id = str(pending["contention_id"])
        with self.assertRaisesRegex(ValueError, "must select wait"):
            contention.propose(
                self.root,
                contention_id=contention_id,
                owner="agent-b",
                run_id="run-data-b",
                epoch=1,
                decision="parallel-tx",
                reason="attempt an isolated branch",
            )
        record = contention.get_decision(resolve(self.root), contention_id)
        self.assertEqual(record["status"], "awaiting-decision")
        self.assertIsNone(record.get("decision"))
