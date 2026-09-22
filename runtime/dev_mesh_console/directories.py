"""Non-recursive, directory-only navigation for the local Console picker."""

from __future__ import annotations

import heapq
import os
from pathlib import Path


DIRECTORY_LIMIT = 200


def browse_directories(
    path: str | None = None, *, query: str = "", show_hidden: bool = False
) -> dict[str, object]:
    if len(query) > 200 or (path is not None and len(path) > 4096):
        raise ValueError("directory query is too long")
    candidate = Path(path).expanduser() if path else Path.home()
    if not candidate.is_absolute():
        raise ValueError("directory path must be absolute")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("select an existing directory")
    match = query.casefold()

    def children():
        with os.scandir(root) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                if match not in entry.name.casefold():
                    continue
                try:
                    # Do not follow aliases while listing. Explicit navigation resolves its path.
                    if entry.is_dir(follow_symlinks=False):
                        yield entry.name
                except OSError:
                    continue

    names = heapq.nsmallest(DIRECTORY_LIMIT + 1, children(), key=lambda name: (name.casefold(), name))
    return {
        "path": str(root),
        "parent": str(root.parent) if root.parent != root else None,
        "home": str(Path.home()),
        "breadcrumbs": [{"name": item.name or str(item), "path": str(item)}
                        for item in [*reversed(root.parents), root]],
        "directories": [{"name": name, "path": str(root / name)} for name in names[:DIRECTORY_LIMIT]],
        "truncated": len(names) > DIRECTORY_LIMIT,
        "limit": DIRECTORY_LIMIT,
    }
