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
    return discover_and_collect(store, roots=roots, max_depth=max_depth)


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
