#!/usr/bin/env python3
"""Run the repository-owned Dev Mesh Web Console."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPOSITORY_ROOT / "runtime"

if not (RUNTIME_ROOT / "dev_mesh_console" / "__init__.py").is_file():
    raise SystemExit(f"Dev Mesh Console runtime is unavailable: {RUNTIME_ROOT}")

sys.path.insert(0, str(RUNTIME_ROOT))

from dev_mesh_console.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
