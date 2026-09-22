from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "install_console_service", ROOT / "scripts/install_console_service.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load Console service installer")
INSTALLER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INSTALLER
SPEC.loader.exec_module(INSTALLER)


class ConsoleServiceInstallerTest(unittest.TestCase):
    def test_launch_agent_passes_discovery_maintenance_interval(self) -> None:
        payload = INSTALLER.launch_agent_payload(
            root=ROOT,
            python=Path(sys.executable),
            database=Path("/tmp/dev-mesh.sqlite3"),
            registry=Path("/tmp/dev-mesh.roots.json"),
            port=8765,
            collect_interval=15,
            discovery_maintenance_interval=600,
        )

        arguments = payload["ProgramArguments"]
        index = arguments.index("--discovery-maintenance-interval")
        self.assertEqual(arguments[index + 1], "600")


if __name__ == "__main__":
    unittest.main()
