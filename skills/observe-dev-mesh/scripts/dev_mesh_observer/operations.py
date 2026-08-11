"""Shared Observer discovery and collection operations."""

from __future__ import annotations

from pathlib import Path

from .catalog import WorkspaceSource, discover_workspaces, ensure_external_data_dir
from .store import ObserverStore


def empty_discovery() -> dict[str, object]:
    return {
        "roots": [],
        "discovered": 0,
        "registered": 0,
        "existing": 0,
        "new_workspace_ids": [],
        "workspaces": [],
    }


def register_discovery(
    store: ObserverStore,
    roots: list[Path],
    sources: list[WorkspaceSource],
) -> dict[str, object]:
    registered_roots = store.register_roots(roots)
    result = store.register_workspaces(sources)
    return {
        "roots": registered_roots,
        **result,
        "workspaces": [source.as_record() for source in sources],
    }


def collect_registered(
    store: ObserverStore,
    *,
    max_depth: int,
) -> dict[str, object]:
    roots = store.scan_roots()
    if not roots:
        return {
            "discovery": empty_discovery(),
            "collection": store.collect(),
        }
    available_roots = [
        root for root in roots if not root.is_symlink() and root.is_dir()
    ]
    unavailable_roots = [
        str(root) for root in roots if root not in available_roots
    ]
    if not available_roots:
        discovery = empty_discovery()
        discovery["roots"] = [str(root) for root in roots]
        discovery["unavailable_roots"] = unavailable_roots
        return {"discovery": discovery, "collection": store.collect()}
    result = discover_and_collect(
        store,
        roots=available_roots,
        max_depth=max_depth,
    )
    result["discovery"]["unavailable_roots"] = unavailable_roots
    return result


def discover_and_collect(
    store: ObserverStore,
    *,
    roots: list[Path],
    max_depth: int,
) -> dict[str, object]:
    """Allowlist roots, discover coordination sources, then ingest events."""

    sources = discover_workspaces(roots, max_depth=max_depth)
    ensure_external_data_dir(store.data_dir, sources)
    discovery = register_discovery(store, roots, sources)
    return {
        "discovery": discovery,
        "collection": store.collect(),
    }
