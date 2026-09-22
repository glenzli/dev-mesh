from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).parents[2]
MODULE_PATH = REPOSITORY / "scripts" / "plugin_dist.py"
SPEC = importlib.util.spec_from_file_location("plugin_dist", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
plugin_dist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin_dist)


class PluginPackagingTest(unittest.TestCase):
    def test_dist_is_minimal_provenance_bound_and_syncable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "dist" / "dev-mesh"
            metadata = plugin_dist.build_package(
                REPOSITORY,
                package,
                revision="abcdef1234567890abcdef1234567890abcdef12",
                require_clean=False,
            )
            self.assertEqual(metadata["version"], "0.2.5")
            self.assertEqual(
                metadata["source_revision"],
                "abcdef1234567890abcdef1234567890abcdef12",
            )
            self.assertEqual(
                json.loads((package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"],
                metadata["version"],
            )
            self.assertTrue((package / "runtime" / "dev_mesh_coord" / "cli.py").is_file())
            self.assertTrue((package / "contracts" / "dev-mesh-coordination-20260823.1.md").is_file())
            self.assertTrue((package / "assets" / "dev-mesh.png").is_file())
            self.assertFalse((package / "assets" / "dev-mesh.svg").exists())
            self.assertFalse((package / "docs" / "assets" / "dev-mesh-icon-original.png").exists())
            packaged_manifest = json.loads(
                (package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            self.assertEqual(packaged_manifest["interface"]["composerIcon"], "./assets/dev-mesh.png")
            self.assertFalse((package / "runtime" / "tests").exists())
            self.assertFalse((package / "contracts" / "archive").exists())
            self.assertFalse((package / ".dev-mesh").exists())

            # Check the producer and guidance in the distributable, not just its manifest version.
            for relative in (
                "runtime/dev_mesh_coord/canonical_git.py",
                "runtime/dev_mesh_coord/cli.py",
                "runtime/dev_mesh_coord/cli_output.py",
                "runtime/dev_mesh_coord/cross_project.py",
                "skills/coordinate-shared-workspace/SKILL.md",
                "skills/coordinate-shared-workspace/references/claim-options.md",
                "skills/coordinate-shared-workspace/references/communication.md",
                "skills/coordinate-shared-workspace/references/direct-publication.md",
            ):
                self.assertEqual((package / relative).read_bytes(), (REPOSITORY / relative).read_bytes())
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            launcher = package / "skills/coordinate-shared-workspace/scripts/coord.py"
            help_result = subprocess.run(
                [sys.executable, str(launcher), "record-message", "--help"],
                cwd=root, env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--source-run-id", help_result.stdout)

            marketplace = root / "marketplace"
            catalog = marketplace / ".agents" / "plugins" / "marketplace.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({"plugins": [{"name": "dev-mesh"}]}), encoding="utf-8")
            stale = marketplace / "plugins" / "dev-mesh"
            stale.mkdir(parents=True)
            (stale / "obsolete.txt").write_text("obsolete", encoding="utf-8")
            synced = plugin_dist.sync_package(package, marketplace, replace=True)
            self.assertEqual(synced["tree_sha256"], metadata["tree_sha256"])
            self.assertFalse((stale / "obsolete.txt").exists())
            self.assertFalse((stale / "runtime" / "tests").exists())


if __name__ == "__main__":
    unittest.main()
