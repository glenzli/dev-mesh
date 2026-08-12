from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {completed.stderr}")
    return completed.stdout.strip()


class GitWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Dev Mesh Test")
        git(self.root, "config", "user.email", "dev-mesh@example.invalid")
        (self.root / "app.txt").write_text("base\n", encoding="utf-8")
        (self.root / "other.txt").write_text("other\n", encoding="utf-8")
        git(self.root, "add", "app.txt", "other.txt")
        git(self.root, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()
