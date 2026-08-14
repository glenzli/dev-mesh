from __future__ import annotations

from dev_mesh_coord.control_plane import initialize
from dev_mesh_coord.workspace_projection import (
    declared_projection,
    workspace_bytes_projection,
)

from helpers import GitWorkspaceTest, git


class WorkspaceProjectionTest(GitWorkspaceTest):
    def test_declared_digest_ignores_unrelated_head_changes(self) -> None:
        plane = initialize(self.root)
        (self.root / "app.txt").write_text("base\nshared\n", encoding="utf-8")
        first = declared_projection(
            self.root, plane, ["app.txt"], git(self.root, "rev-parse", "HEAD"), require_changes=True
        )
        git(self.root, "add", "other.txt")
        (self.root / "other.txt").write_text("other\nnew\n", encoding="utf-8")
        git(self.root, "add", "other.txt")
        git(self.root, "commit", "-m", "unrelated")
        second = declared_projection(
            self.root, plane, ["app.txt"], git(self.root, "rev-parse", "HEAD"), require_changes=True
        )
        self.assertEqual(first["declared_content_sha256"], second["declared_content_sha256"])
        self.assertNotEqual(first["expected_index_tree"], second["expected_index_tree"])

    def test_future_absent_path_does_not_break_git_projection(self) -> None:
        plane = initialize(self.root)
        (self.root / "app.txt").write_text("base\nchanged\n", encoding="utf-8")
        projection = declared_projection(
            self.root,
            plane,
            ["app.txt", "future.txt"],
            git(self.root, "rev-parse", "HEAD"),
            require_changes=True,
        )
        self.assertEqual(projection["actual_paths"], ["app.txt"])

    def test_workspace_bytes_hashes_only_explicit_ignored_regular_files(self) -> None:
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-m", "ignore local data")
        local = self.root / "local"
        local.mkdir()
        (local / "state.json").write_text('{"value":1}\n', encoding="utf-8")

        first = workspace_bytes_projection(
            self.root, ["local/state.json", "local/future.json"]
        )
        self.assertEqual(first["projection_mode"], "workspace-bytes")
        self.assertEqual(first["workspace_file_count"], 1)
        self.assertEqual(first["workspace_missing_path_count"], 1)
        self.assertNotIn("{\"value\":1}", repr(first))

        (local / "state.json").write_text('{"value":2}\n', encoding="utf-8")
        second = workspace_bytes_projection(
            self.root, ["local/state.json", "local/future.json"]
        )
        self.assertNotEqual(
            first["workspace_bytes_sha256"], second["workspace_bytes_sha256"]
        )

        with self.assertRaisesRegex(ValueError, "explicitly ignored"):
            workspace_bytes_projection(self.root, ["ordinary-new.json"])
        with self.assertRaisesRegex(ValueError, "tracked by Git"):
            workspace_bytes_projection(self.root, ["app.txt"])
