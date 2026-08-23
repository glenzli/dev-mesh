#!/usr/bin/env python3
"""Build and explicitly synchronize a minimal, provenance-bound Dev Mesh plugin package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PLUGIN_NAME = "dev-mesh"
PACKAGE_SCHEMA = 1
TREE_DIRECTORIES = (
    "assets",
    "skills",
    "runtime/dev_mesh_coord",
    "runtime/dev_mesh_console",
    "runtime/dev_mesh_observer",
)
PACKAGE_FILES = (
    ".codex-plugin/plugin.json",
    "runtime/pyproject.toml",
    "schemas/event.schema.json",
    "schemas/current.schema.json",
    "contracts/dev-mesh-coordination-20260823.1.md",
    "contracts/dev-mesh-cross-project-collaboration-20260823.1.md",
    "contracts/dev-mesh-observer-status-20260812.1.md",
)
FORBIDDEN_PARTS = frozenset({".dev-mesh", "archive", "tests", "__pycache__"})


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def source_revision(root: Path) -> str:
    revision = run_git(root, "rev-parse", "--verify", "HEAD")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("source revision must be a full SHA-1 Git commit")
    return revision


def require_clean_source(root: Path) -> None:
    dirty = run_git(root, "status", "--porcelain", "--untracked-files=normal", "--ignored=no")
    if dirty:
        raise ValueError("plugin dist must be built from a clean source tree")


def copy_file(root: Path, relative: str, destination: Path) -> None:
    source = root / relative
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"required package file is missing or unsafe: {relative}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(root: Path, relative: str, destination: Path) -> None:
    source = root / relative
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"required package directory is missing or unsafe: {relative}")

    def ignore_junk(_: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in FORBIDDEN_PARTS or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source, destination / relative, symlinks=False, ignore=ignore_junk)


def manifest_version(root: Path) -> tuple[dict[str, Any], str]:
    manifest_path = root / ".codex-plugin" / "plugin.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("name") != PLUGIN_NAME:
        raise ValueError("source plugin manifest has an unexpected name")
    version = payload.get("version")
    if not isinstance(version, str) or not version or "+" in version:
        raise ValueError("source plugin manifest must contain a base semantic version without build metadata")
    return payload, version


def tree_digest(package_root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = sorted(path for path in package_root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest(), len(files)


def assert_minimal_package(package_root: Path) -> None:
    for path in package_root.rglob("*"):
        relative = path.relative_to(package_root)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError(f"forbidden package content: {relative.as_posix()}")
        if path.is_symlink():
            raise ValueError(f"package may not contain symlinks: {relative.as_posix()}")
    manifest = package_root / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        raise ValueError("package manifest is missing")


def release_metadata_path(package_root: Path) -> Path:
    return package_root.parent / f"{package_root.name}.release.json"


def build_package(root: Path, output: Path, *, revision: str, require_clean: bool) -> dict[str, Any]:
    root = root.resolve()
    output = output.resolve()
    if require_clean:
        require_clean_source(root)
    manifest, base_version = manifest_version(root)
    if len(revision) < 7 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("release revision must be a lowercase Git hexadecimal identifier")
    version = f"{base_version}+codex.{revision[:12]}"
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for relative in PACKAGE_FILES:
            copy_file(root, relative, staging)
        for relative in TREE_DIRECTORIES:
            copy_tree(root, relative, staging)
        package_manifest = json.loads((staging / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        package_manifest["version"] = version
        (staging / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(package_manifest, indent=2) + "\n", encoding="utf-8"
        )
        assert_minimal_package(staging)
        digest, file_count = tree_digest(staging)
        metadata = {
            "schema": PACKAGE_SCHEMA,
            "plugin": PLUGIN_NAME,
            "source_revision": revision,
            "version": version,
            "tree_sha256": digest,
            "file_count": file_count,
        }
        if output.exists():
            shutil.rmtree(output)
        os.replace(staging, output)
        release_metadata_path(output).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return metadata
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def load_verified_release(package_root: Path) -> dict[str, Any]:
    package_root = package_root.resolve()
    metadata_path = release_metadata_path(package_root)
    if not metadata_path.is_file():
        raise ValueError(f"missing release metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("schema") != PACKAGE_SCHEMA:
        raise ValueError("release metadata has an unsupported schema")
    assert_minimal_package(package_root)
    digest, file_count = tree_digest(package_root)
    if metadata.get("plugin") != PLUGIN_NAME or metadata.get("tree_sha256") != digest:
        raise ValueError("release package does not match its recorded tree digest")
    if metadata.get("file_count") != file_count:
        raise ValueError("release package file count does not match its recorded metadata")
    manifest = json.loads((package_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME or manifest.get("version") != metadata.get("version"):
        raise ValueError("release package manifest does not match release metadata")
    return metadata


def sync_package(package_root: Path, marketplace_root: Path, *, replace: bool) -> dict[str, Any]:
    metadata = load_verified_release(package_root)
    marketplace_root = marketplace_root.resolve()
    catalog_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    plugin_names = {entry.get("name") for entry in catalog.get("plugins", []) if isinstance(entry, dict)}
    if PLUGIN_NAME not in plugin_names:
        raise ValueError("marketplace does not register the Dev Mesh plugin")
    plugins_root = (marketplace_root / "plugins").resolve()
    target = plugins_root / PLUGIN_NAME
    if target.parent != plugins_root or target.name != PLUGIN_NAME:
        raise ValueError("unsafe marketplace package destination")
    if target.exists() and not replace:
        raise ValueError("marketplace package already exists; pass --replace after review")
    staging = Path(tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}.", dir=plugins_root))
    try:
        shutil.copytree(package_root, staging / PLUGIN_NAME, symlinks=False)
        assert_minimal_package(staging / PLUGIN_NAME)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging / PLUGIN_NAME, target)
        return {"target": str(target), **metadata}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build a minimal plugin package from clean source")
    build.add_argument("--output", type=Path, default=repository_root() / "dist" / PLUGIN_NAME)
    sync = commands.add_parser("sync", help="copy a verified dist package into a marketplace working tree")
    sync.add_argument("--package", type=Path, default=repository_root() / "dist" / PLUGIN_NAME)
    sync.add_argument("--marketplace-root", type=Path, required=True)
    sync.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repository_root()
    if args.command == "build":
        output = args.output.resolve()
        dist_root = (root / "dist").resolve()
        if output != dist_root / PLUGIN_NAME:
            raise ValueError("build output must be dist/dev-mesh inside this repository")
        metadata = build_package(root, output, revision=source_revision(root), require_clean=True)
    else:
        metadata = sync_package(args.package, args.marketplace_root, replace=args.replace)
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
