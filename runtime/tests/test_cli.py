from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from helpers import GitWorkspaceTest, git
from dev_mesh_coord.errors import STABLE_ERROR_CODES, error_json


RUNTIME = Path(__file__).parents[1]
REPOSITORY = Path(__file__).parents[2]


class CliTest(GitWorkspaceTest):
    def test_protocol_error_codes_are_exhaustive_and_documented(self) -> None:
        package = Path(__file__).parents[1] / "dev_mesh_coord"
        emitted: set[str] = set()
        for source in package.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ProtocolError"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    emitted.add(node.args[0].value)
        self.assertLessEqual(emitted, STABLE_ERROR_CODES)
        self.assertNotIn("legacy_retired", STABLE_ERROR_CODES)
        contract = (REPOSITORY / "contracts/dev-mesh-coordination-20260814.1.md").read_text(
            encoding="utf-8"
        )
        stable_section = contract.split("## Stable control-plane failures\n", 1)[1]
        documented = set(re.findall(r"^- `([^`]+)`$", stable_section, flags=re.MULTILINE))
        self.assertEqual(documented, STABLE_ERROR_CODES)
        self.assertEqual(
            json.loads(error_json(RuntimeError("bounded failure")))["error"]["code"],
            "operation_failed",
        )

    def _run(self, module: str, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(RUNTIME)
        completed = subprocess.run(
            (sys.executable, "-m", module, *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def test_claim_cli_accepts_explicit_workspace_bytes_projection(self) -> None:
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "ignore local data")
        root = str(self.root)
        self._run("dev_mesh_coord", "--root", root, "init")
        self._run(
            "dev_mesh_coord",
            "--root",
            root,
            "join",
            "--owner",
            "cli-agent",
            "--run-id",
            "cli-run",
            "--task",
            "write ignored data",
        )
        claim = json.loads(
            self._run(
                "dev_mesh_coord",
                "--root",
                root,
                "claim",
                "--scope",
                "cli-local-data",
                "--owner",
                "cli-agent",
                "--run-id",
                "cli-run",
                "--task",
                "write ignored data",
                "--path",
                "local/state.json",
                "--projection-mode",
                "workspace-bytes",
            ).stdout
        )
        self.assertEqual(claim["status"], "active")
        self.assertEqual(claim["projection_mode"], "workspace-bytes")

    def test_json_cli_and_observer_vertical_slice(self) -> None:
        root = str(self.root)
        initialized = self._run("dev_mesh_coord", "--root", root, "init")
        self.assertEqual(json.loads(initialized.stdout)["protocol"], "20260814.1")
        self._run(
            "dev_mesh_coord",
            "--root",
            root,
            "join",
            "--owner",
            "cli-agent",
            "--run-id",
            "cli-run",
            "--task",
            "CLI smoke",
        )
        self._run(
            "dev_mesh_coord",
            "--root",
            root,
            "claim",
            "--scope",
            "cli-read",
            "--owner",
            "cli-agent",
            "--run-id",
            "cli-run",
            "--task",
            "read app",
            "--path",
            "app.txt",
            "--intent",
            "read",
        )
        self._run(
            "dev_mesh_coord",
            "--root",
            root,
            "claim-pause",
            "--scope",
            "cli-read",
            "--owner",
            "cli-agent",
            "--run-id",
            "cli-run",
            "--blocker-kind",
            "dependency",
            "--checkpoint",
            "waiting for a declared dependency",
            "--resume-condition",
            "dependency publishes its result",
        )
        self._run(
            "dev_mesh_coord",
            "--root",
            root,
            "claim-resume",
            "--scope",
            "cli-read",
            "--owner",
            "cli-agent",
            "--run-id",
            "cli-run",
            "--evidence",
            "dependency result was inspected",
        )
        status = json.loads(
            self._run(
                "dev_mesh_coord",
                "--root",
                root,
                "status",
                "--owner",
                "cli-agent",
                "--run-id",
                "cli-run",
            ).stdout
        )
        self.assertEqual(status["protocol"], "20260814.1")
        self.assertEqual(status["claims"]["sample"][0]["scope"], "cli-read")
        verbose_status = json.loads(
            self._run(
                "dev_mesh_coord",
                "--root",
                root,
                "--verbose",
                "status",
                "--owner",
                "cli-agent",
                "--run-id",
                "cli-run",
            ).stdout
        )
        self.assertEqual(verbose_status["runs"][0]["run_id"], "cli-run")
        self.assertEqual(verbose_status["claims"][0]["scope"], "cli-read")
        self.assertIn("paths", verbose_status["claims"][0])

        database = Path(self.temporary.name) / "cli-observer.sqlite3"
        collected = self._run(
            "dev_mesh_observer",
            "--db",
            str(database),
            "collect",
            "--root",
            root,
        )
        self.assertEqual(json.loads(collected.stdout)["workspace_count"], 1)
        report = self._run("dev_mesh_observer", "--db", str(database), "report")
        self.assertEqual(json.loads(report.stdout)["protocol_version"], "20260814.1")

    def test_no_alternate_state_directory_escape_hatch(self) -> None:
        failed = self._run(
            "dev_mesh_coord",
            "--root",
            str(self.root),
            "--state-dir",
            str(Path(self.temporary.name) / "alternate"),
            "status",
            expected=2,
        )
        self.assertNotIn("state-dir", failed.stderr.splitlines()[0])
        self.assertIn("error:", failed.stderr)

    def test_managed_direct_commit_cli_vertical_slice(self) -> None:
        root = str(self.root)
        self._run("dev_mesh_coord", "--root", root, "init")
        self._run(
            "dev_mesh_coord", "--root", root, "join",
            "--owner", "cli-writer", "--run-id", "cli-write-run",
            "--task", "write through the canonical boundary",
        )
        self._run(
            "dev_mesh_coord", "--root", root, "claim",
            "--scope", "cli-write", "--owner", "cli-writer",
            "--run-id", "cli-write-run", "--task", "update app",
            "--path", "app.txt", "--intent", "local-edit",
        )
        (self.root / "app.txt").write_text("managed direct commit\n", encoding="utf-8")
        result = json.loads(
            self._run(
                "dev_mesh_coord", "--root", root, "direct-commit",
                "--scope", "cli-write", "--owner", "cli-writer",
                "--run-id", "cli-write-run", "--summary", "update app",
                "--validation-evidence", "CLI vertical slice passed",
            ).stdout
        )
        self.assertEqual(result["status"], "completed")
        doctor = json.loads(
            self._run("dev_mesh_coord", "--root", root, "direct-commit-doctor").stdout
        )
        self.assertEqual(doctor["active_direct_commits"]["count"], 0)
        self.assertEqual(
            subprocess.run(
                ("git", "-C", root, "show", "HEAD:app.txt"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout,
            "managed direct commit\n",
        )
