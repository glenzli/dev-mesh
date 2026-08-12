"""Durable scan-root configuration outside observed workspaces."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable


class RootRegistry:
    def __init__(self, path: Path, initial_roots: Iterable[Path] = ()):
        self.path = path.expanduser().resolve()
        if ".dev-mesh" in self.path.parts or ".agent-coordination" in self.path.parts:
            raise ValueError("Console root registry must remain outside coordination state")
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._roots = self._load()
        changed = False
        for root in initial_roots:
            normalized = self._normalize(root)
            if normalized not in self._roots:
                self._roots.append(normalized)
                changed = True
        self._roots.sort()
        if changed or not self.path.exists():
            self._write()

    @staticmethod
    def _normalize(value: Path | str) -> str:
        root = Path(value).expanduser().resolve()
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"scan root must be an existing regular directory: {root}")
        return str(root)

    def _load(self) -> list[str]:
        if not self.path.exists():
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("Console root registry must be a regular file")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read Console root registry: {error}") from error
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("unsupported Console root registry")
        roots = value.get("roots")
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            raise ValueError("Console root registry roots must be strings")
        normalized = sorted({self._normalize(item) for item in roots})
        return normalized

    def _write(self) -> None:
        encoded = (
            json.dumps(
                {"schema": 1, "roots": self._roots},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def roots(self) -> list[Path]:
        with self._lock:
            return [Path(item) for item in self._roots]

    def add(self, value: Path | str) -> list[Path]:
        normalized = self._normalize(value)
        with self._lock:
            if normalized not in self._roots:
                self._roots.append(normalized)
                self._roots.sort()
                self._write()
            return [Path(item) for item in self._roots]

    def remove(self, value: Path | str) -> list[Path]:
        normalized = str(Path(value).expanduser().resolve())
        with self._lock:
            if normalized in self._roots:
                self._roots.remove(normalized)
                self._write()
            return [Path(item) for item in self._roots]
