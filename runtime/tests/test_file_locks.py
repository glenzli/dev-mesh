from __future__ import annotations

import errno
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dev_mesh_coord import file_locks, git_effects
from dev_mesh_coord.control_plane import ControlPlane
from dev_mesh_coord.process_runner import run
from dev_mesh_coord.storage import advisory_lock, json_bytes, read_json, replace_json, write_json_exclusive


class FileLocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "locks").mkdir()
        self.plane = ControlPlane(self.root, self.root, "20260823.1", 2)

    def test_exclusion_timeout_and_release(self) -> None:
        path = self.root / "shared.lock"
        with advisory_lock(path):
            with self.assertRaises(TimeoutError):
                with advisory_lock(path, timeout_seconds=0.01):
                    self.fail("a competing caller acquired the lock")
        with advisory_lock(path):
            self.assertTrue(path.is_file())

    def test_permissions_are_not_classified_as_an_in_flight_effect(self) -> None:
        with mock.patch.object(file_locks, "acquire", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(PermissionError):
                git_effects.is_canonical_in_flight(self.plane)
            with self.assertRaises(PermissionError):
                with git_effects.canonical_fence(self.plane):
                    self.fail("permission failure granted a fence")
            with self.assertRaises(PermissionError):
                with advisory_lock(self.root / "denied.lock"):
                    self.fail("permission failure granted a lock")

    def test_child_inherits_only_declared_fences_after_parent_is_killed(self) -> None:
        # Exercise the same runner/fences used by managed Git on both platforms.
        child_code = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[1])\n"
            "(root / 'child.pid').write_text(str(os.getpid()))\n"
            "deadline = time.monotonic() + 30\n"
            "while not (root / 'release').exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n"
        )
        self._assert_surviving_child([sys.executable, "-c", child_code, str(self.root)])

    def test_native_git_retains_fences_when_publisher_is_killed(self) -> None:
        from helpers import git

        if shutil.which("git") is None:
            self.skipTest("Git is unavailable")
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Dev Mesh Test")
        git(self.root, "config", "user.email", "dev-mesh@example.invalid")
        (self.root / "app.txt").write_text("fenced commit\n", encoding="utf-8")
        git(self.root, "add", "app.txt")
        hook_code = (
            "import os, time; from pathlib import Path; "
            "Path('child.pid').write_text(str(os.getpid())); "
            "deadline = time.monotonic() + 30\n"
            "while not Path('release').exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
        )
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\nexec " + shlex.quote(Path(sys.executable).as_posix())
            + " -c " + shlex.quote(hook_code) + "\n", encoding="utf-8", newline="\n",
        )
        hook.chmod(0o755)
        self._assert_surviving_child(["git", "-C", str(self.root), "commit", "-m", "fenced commit"])
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "fenced commit")

    def _assert_surviving_child(self, child_arguments: list[str]) -> None:
        parent_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from dev_mesh_coord import git_effects\n"
            "from dev_mesh_coord.control_plane import ControlPlane\n"
            "from dev_mesh_coord.process_runner import run\n"
            "root = Path(sys.argv[1])\n"
            "plane = ControlPlane(root, root, '20260823.1', 2)\n"
            "with git_effects.canonical_fence(plane) as canonical:\n"
            "    with git_effects.transaction_fence(plane, 'tx-child') as transaction:\n"
            f"        run({child_arguments!r}, pass_fds=(canonical, transaction), cwd=root)\n"
        )
        environment = dict(os.environ)
        runtime = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = runtime + os.pathsep + environment.get("PYTHONPATH", "")
        with (self.root / "parent.log").open("wb") as log:
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_code, str(self.root)],
                env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while not (self.root / "child.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(
                    (self.root / "child.pid").exists(),
                    "fenced child did not start; "
                    f"parent_rc={parent.poll()} log="
                    + (self.root / "parent.log").read_text(encoding="utf-8", errors="replace"),
                )
                self.assertTrue(git_effects.is_canonical_in_flight(self.plane))
                self.assertTrue(git_effects.is_in_flight(self.plane, "tx-child"))
                parent.kill()
                parent.wait(timeout=5)
                self.assertTrue(git_effects.is_canonical_in_flight(self.plane))
                self.assertTrue(git_effects.is_in_flight(self.plane, "tx-child"))
                with self.assertRaises(git_effects.GitEffectInFlight):
                    with git_effects.canonical_fence(self.plane):
                        self.fail("surviving child lost its canonical fence")
            finally:
                (self.root / "release").touch()
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                deadline = time.monotonic() + 10
                while git_effects.is_canonical_in_flight(self.plane) and time.monotonic() < deadline:
                    time.sleep(0.02)
            self.assertFalse(git_effects.is_canonical_in_flight(self.plane))
            self.assertFalse(git_effects.is_in_flight(self.plane, "tx-child"))

    def test_runner_preserves_input_output_failure_and_handle_flags(self) -> None:
        descriptor = file_locks.acquire(self.root / "process.lock")
        try:
            previous = os.get_inheritable(descriptor)
            result = run(
                [sys.executable, "-X", "utf8", "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
                pass_fds=(descriptor,), input="目录 with spaces", capture_output=True,
                text=True, encoding="utf-8",
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout.strip(), "目录 with spaces")
            self.assertEqual(os.get_inheritable(descriptor), previous)
            with self.assertRaises(subprocess.CalledProcessError):
                run([sys.executable, "-c", "raise SystemExit(3)"], pass_fds=(descriptor,), check=True)
            with self.assertRaises(OSError):
                run([str(self.root / "missing-program")], pass_fds=(descriptor,))
            self.assertEqual(os.get_inheritable(descriptor), previous)
        finally:
            file_locks.release(descriptor)

    def test_json_publication_is_exclusive_and_reopenable(self) -> None:
        path = self.root / "record.json"
        first, second = {"value": "目录"}, {"value": "updated"}
        write_json_exclusive(path, first, base=self.root)
        with self.assertRaises(FileExistsError):
            write_json_exclusive(path, second, base=self.root)
        self.assertEqual(path.read_bytes(), json_bytes(first))
        replace_json(path, second, base=self.root)
        self.assertEqual(read_json(path, base=self.root), second)
        self.assertEqual(sorted(item.name for item in self.root.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Win32 filename aliases")
    def test_windows_rejects_stream_and_device_record_names(self) -> None:
        for name in ("run:stream.json", "CON.json", "nul.json", "COM1.json", "LPT¹.json", "record.json."):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "portable id"):
                write_json_exclusive(self.root / name, {"value": 1}, base=self.root)
        self.assertEqual(list(self.root.glob("*.json")), [])

    @unittest.skipUnless(os.name == "nt", "Win32 case aliases")
    def test_windows_rejects_case_aliases_of_existing_records(self) -> None:
        write_json_exclusive(self.root / "Run-A.json", {"run_id": "Run-A"}, base=self.root)
        with self.assertRaisesRegex(ValueError, "filename case"):
            read_json(self.root / "run-a.json", base=self.root)
        with self.assertRaisesRegex(ValueError, "filename case"):
            replace_json(self.root / "run-a.json", {"run_id": "run-a"}, base=self.root)
        self.assertEqual(read_json(self.root / "Run-A.json", base=self.root), {"run_id": "Run-A"})
