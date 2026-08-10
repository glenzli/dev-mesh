"""Shared repository harness for transaction integration boundaries."""

from __future__ import annotations

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


class TransactionRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Coordination Test")
        self.run_git("config", "user.email", "coordination@example.test")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "router.txt").write_text(
            "health=off\n"
            + "padding\n" * 20
            + "metrics=off\n",
            encoding="utf-8",
        )
        (self.repo / "notes.txt").write_text("base\n", encoding="utf-8")
        self.run_git("add", "src/router.txt", "notes.txt")
        self.run_git("commit", "-m", "initial")
        self.run_tx("init", "--steward", "central")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(
        self,
        *arguments: str,
        expected: int = 0,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_environment = os.environ.copy()
        if environment:
            command_environment.update(environment)
        completed = subprocess.run(
            list(arguments),
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=command_environment,
        )
        if completed.returncode != expected:
            self.fail(
                f"command returned {completed.returncode}, expected {expected}: "
                f"{' '.join(arguments)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    def run_git(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        return self.run_command("git", *arguments, expected=expected)

    def run_coord(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            "python3",
            str(COORD),
            *arguments,
            "--root",
            str(self.repo),
            expected=expected,
        )

    def run_tx(
        self,
        *arguments: str,
        expected: int = 0,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            "python3",
            str(TX),
            *arguments,
            "--root",
            str(self.repo),
            expected=expected,
            environment=environment,
        )

    def claim(
        self,
        scope: str,
        owner: str,
        semantic_write: str,
        intent: str = "additive",
    ) -> None:
        self.run_coord(
            "claim",
            "--scope",
            scope,
            "--owner",
            owner,
            "--task",
            f"Implement {scope}",
            "--paths",
            "src/router.txt",
            "--first-release",
            f"{scope} is validated",
            "--intent",
            intent,
            "--semantic-writes",
            semantic_write,
            "--sensitive-to",
            "contract:routing",
            "--validation",
            "focused routing test",
            "--allow-overlap",
            "--pending-on-conflict",
            "--reason",
            "Pending semantic arbitration; do not write the overlap",
        )

    def begin_pair(self, mode: str = "parallel-tx") -> list[dict[str, object]]:
        completed = self.run_tx(
            "begin",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            mode,
            "--steward",
            "central",
            "--reason",
            "Independent route entries",
        )
        return json.loads(completed.stdout)

    def crash_begin(self, point: str) -> tuple[Path, dict[str, object]]:
        self.run_tx(
            "begin",
            "--scopes",
            "health",
            "metrics",
            "--mode",
            "parallel-tx",
            "--steward",
            "central",
            "--reason",
            "Independent route entries",
            expected=86,
            environment={"SHARED_COORD_TEST_CRASH_POINT": point},
        )
        paths = list(
            (self.repo / ".agent-coordination" / "groups" / "active").glob("*.json")
        )
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def prepare_validate(self, record: dict[str, object], summary: str) -> None:
        transaction_id = str(record["transaction_id"])
        owner = str(record["owner"])
        self.run_tx(
            "prepare",
            "--transaction",
            transaction_id,
            "--owner",
            owner,
            "--summary",
            summary,
        )
        self.run_tx(
            "validate",
            "--transaction",
            transaction_id,
            "--owner",
            owner,
            "--evidence",
            "focused test passed",
        )
