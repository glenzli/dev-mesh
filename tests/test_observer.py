from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORD = (
    PROJECT_ROOT
    / "skills"
    / "coordinate-shared-workspace"
    / "scripts"
    / "coord.py"
)
TX = (
    PROJECT_ROOT
    / "skills"
    / "coordinate-shared-workspace"
    / "scripts"
    / "tx.py"
)
OBSERVE = (
    PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts" / "observe.py"
)


class ObserverIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.scan_root = self.base / "workspaces"
        self.scan_root.mkdir()
        self.data_dir = self.base / "observer-data"
        self.repo_a = self.create_repository("repo-a")
        self.repo_b = self.create_repository("repo-b")
        self.seed_events()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(
        self,
        *arguments: str,
        cwd: Path | None = None,
        expected: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd or self.base,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        if completed.returncode != expected:
            self.fail(
                f"command returned {completed.returncode}, expected {expected}: "
                f"{' '.join(arguments)}\nstdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        return completed

    def create_repository(self, name: str) -> Path:
        repository = self.scan_root / name
        repository.mkdir()
        self.run_command("git", "init", "-b", "main", cwd=repository)
        self.run_command(
            "git", "config", "user.name", "Observer Test", cwd=repository
        )
        self.run_command(
            "git",
            "config",
            "user.email",
            "observer@example.test",
            cwd=repository,
        )
        (repository / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        self.run_command("git", "add", "README.md", cwd=repository)
        self.run_command("git", "commit", "-m", "initial", cwd=repository)
        self.run_command(
            "python3",
            str(TX),
            "init",
            "--steward",
            "observer-test",
            "--root",
            str(repository),
        )
        return repository

    def run_coord(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            "python3",
            str(COORD),
            *arguments,
            "--root",
            str(repository),
        )

    def run_observer(
        self,
        *arguments: str,
        expected: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            "python3",
            str(OBSERVE),
            "--data-dir",
            str(self.data_dir),
            *arguments,
            expected=expected,
        )

    def seed_events(self) -> None:
        self.run_coord(
            self.repo_a,
            "agent-join",
            "--run",
            "run-a",
            "--owner",
            "agent-a",
            "--task",
            "Complete repository A task",
        )
        self.run_coord(
            self.repo_a,
            "agent-leave",
            "--run",
            "run-a",
            "--owner",
            "agent-a",
            "--outcome",
            "completed",
            "--summary",
            "Repository A task completed",
        )
        self.run_coord(
            self.repo_b,
            "agent-join",
            "--run",
            "run-b",
            "--owner",
            "agent-b",
            "--task",
            "Prepare repository B handoff",
        )
        self.run_coord(
            self.repo_b,
            "agent-join",
            "--run",
            "run-c",
            "--owner",
            "agent-c",
            "--task",
            "Receive repository B handoff",
            "--parent-owner",
            "agent-b",
        )
        self.run_coord(
            self.repo_b,
            "message",
            "--to",
            "agent-c",
            "--from-owner",
            "agent-b",
            "--subject",
            "Continue repository B",
            "--body",
            "Checkpoint is ready for review.",
            "--type",
            "handoff",
            "--requires-ack",
            "--run",
            "run-b",
            "--handoff",
            "repo-b-handoff",
        )

    @staticmethod
    def coordination_snapshot(repository: Path) -> dict[str, str]:
        coordination = repository / ".agent-coordination"
        snapshot: dict[str, str] = {}
        for path in sorted(coordination.rglob("*")):
            if path.is_file() and not path.is_symlink():
                snapshot[path.relative_to(coordination).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return snapshot

    def discover_and_collect(self) -> tuple[dict[str, object], dict[str, object]]:
        discovery = json.loads(
            self.run_observer(
                "discover",
                "--roots",
                str(self.scan_root),
                "--max-depth",
                "3",
            ).stdout
        )
        collection = json.loads(self.run_observer("collect").stdout)
        return discovery, collection

    def test_discovers_collects_and_reports_without_source_writes(self) -> None:
        before = {
            self.repo_a.name: self.coordination_snapshot(self.repo_a),
            self.repo_b.name: self.coordination_snapshot(self.repo_b),
        }
        discovery, first = self.discover_and_collect()

        self.assertEqual(discovery["discovered"], 2)
        self.assertEqual(discovery["registered"], 2)
        self.assertEqual(len(set(discovery["new_workspace_ids"])), 2)
        self.assertEqual(first["collection"]["available"], 2)
        self.assertGreater(first["collection"]["inserted"], 0)
        self.assertEqual(first["collection"]["issues"], 0)

        after = {
            self.repo_a.name: self.coordination_snapshot(self.repo_a),
            self.repo_b.name: self.coordination_snapshot(self.repo_b),
        }
        self.assertEqual(after, before)
        self.assertFalse(str(self.data_dir).startswith(str(self.scan_root)))

        second = json.loads(self.run_observer("collect").stdout)
        self.assertEqual(second["collection"]["inserted"], 0)
        self.assertEqual(
            second["collection"]["skipped"],
            second["collection"]["seen"],
        )

        status = json.loads(self.run_observer("status").stdout)
        self.assertEqual(status["summary"]["workspaces"], 2)
        self.assertEqual(status["summary"]["available"], 2)
        self.assertEqual(
            status["summary"]["events"],
            first["collection"]["inserted"],
        )
        self.assertTrue(
            all(workspace["git_toplevel"] for workspace in status["workspaces"])
        )

        report = json.loads(
            self.run_observer("report", "--since", "7d").stdout
        )
        summary = report["summary"]
        self.assertEqual(summary["registered_workspaces"], 2)
        self.assertEqual(summary["active_workspaces_in_window"], 2)
        self.assertEqual(summary["runs_joined"], 3)
        self.assertEqual(summary["runs_closed"], 1)
        self.assertEqual(summary["runs_open"], 2)
        self.assertEqual(summary["handoffs_offered"], 1)
        self.assertEqual(summary["handoffs_pending"], 1)
        self.assertEqual(report["pending_handoffs"][0]["handoff_id"], "repo-b-handoff")

    def test_changed_immutable_event_is_reported_without_replacement(self) -> None:
        _, first = self.discover_and_collect()
        original_event_count = first["collection"]["inserted"]
        event_path = sorted(
            (self.repo_a / ".agent-coordination" / "events").glob("*.json")
        )[0]
        event = json.loads(event_path.read_text(encoding="utf-8"))
        event["tampered"] = True
        event_path.write_text(
            json.dumps(event, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        changed = json.loads(self.run_observer("collect").stdout)
        self.assertEqual(changed["collection"]["issues"], 1)
        status = json.loads(self.run_observer("status").stdout)
        self.assertEqual(status["summary"]["issues"], 1)
        self.assertEqual(status["summary"]["events"], original_event_count)

        repeated = json.loads(self.run_observer("collect").stdout)
        self.assertEqual(repeated["collection"]["issues"], 1)
        status = json.loads(self.run_observer("status").stdout)
        self.assertEqual(status["summary"]["issues"], 1)

    def test_collect_without_roots_is_an_empty_idempotent_operation(self) -> None:
        empty_data = self.base / "empty-observer"
        completed = self.run_command(
            "python3",
            str(OBSERVE),
            "--data-dir",
            str(empty_data),
            "collect",
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["collection"]["workspaces"], 0)
        self.assertEqual(result["collection"]["inserted"], 0)

    def test_rejects_data_directory_inside_workspace_before_writing(self) -> None:
        bad_data_dir = self.repo_a / "observer-data"
        rejected = self.run_command(
            "python3",
            str(OBSERVE),
            "--data-dir",
            str(bad_data_dir),
            "discover",
            "--roots",
            str(self.repo_a),
            expected=1,
        )
        self.assertIn("outside every discovered workspace", rejected.stderr)
        self.assertFalse(bad_data_dir.exists())

    def test_invalid_report_window_is_rejected(self) -> None:
        rejected = self.run_observer(
            "report",
            "--since",
            "yesterday-ish",
            expected=1,
        )
        self.assertIn("ISO timestamp or duration", rejected.stderr)
