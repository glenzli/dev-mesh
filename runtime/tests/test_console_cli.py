from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dev_mesh_console.cli import main


class ConsoleCliTest(unittest.TestCase):
    def _start(self, supported: bool, *extra: str) -> tuple[int, dict, str, mock.Mock]:
        with tempfile.TemporaryDirectory() as directory:
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch("dev_mesh_console.cli.supported_platform", return_value=supported),
                mock.patch("dev_mesh_console.cli.ConsoleServer") as server,
                mock.patch("dev_mesh_console.cli.ConsoleState"),
                mock.patch("dev_mesh_console.cli.ObserverFacilityService") as facility,
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
            ):
                server.return_value.server_address = ("127.0.0.1", 8765)
                code = main(["--db", str(Path(directory) / "observer.sqlite3"), *extra])
            return code, json.loads(output.getvalue()) if output.getvalue() else {}, errors.getvalue(), facility

    def test_windows_default_runs_console_without_unix_publication(self) -> None:
        code, ready, errors, facility = self._start(False)
        self.assertEqual(code, 0, errors)
        self.assertFalse(ready["infra_discovery"])
        self.assertEqual(ready["infra_discovery_reason"], "unsupported_platform")
        facility.assert_not_called()

    def test_supported_platform_keeps_discovery_and_explicit_opt_out(self) -> None:
        code, ready, errors, facility = self._start(True)
        self.assertEqual(code, 0, errors)
        self.assertTrue(ready["infra_discovery"])
        facility.return_value.start.assert_called_once()
        facility.return_value.stop.assert_called_once()
        code, ready, errors, facility = self._start(True, "--no-infra-discovery")
        self.assertEqual(code, 0, errors)
        self.assertFalse(ready["infra_discovery"])
        self.assertEqual(ready["infra_discovery_reason"], "disabled_by_option")
        facility.assert_not_called()

    def test_explicit_runtime_does_not_silently_enable_unsupported_binding(self) -> None:
        code, ready, errors, facility = self._start(False, "--infra-runtime-dir", "unused")
        self.assertEqual(code, 1)
        self.assertFalse(ready)
        self.assertIn("requires the macOS/Linux", json.loads(errors)["error"]["message"])
        facility.assert_not_called()
