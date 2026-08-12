"""Serialized collection lifecycle shared by Console API requests and the timer."""

from __future__ import annotations

import threading
from pathlib import Path

from dev_mesh_coord.storage import now
from dev_mesh_observer.catalog import Catalog
from dev_mesh_observer.dashboard import build_dashboard

from .registry import RootRegistry


class ConsoleState:
    def __init__(
        self,
        *,
        database: Path,
        registry: RootRegistry,
        max_depth: int,
        collect_interval: float,
    ):
        if max_depth < 0 or max_depth > 12:
            raise ValueError("max depth must be between 0 and 12")
        if collect_interval < 1 or collect_interval > 3600:
            raise ValueError("collect interval must be between 1 and 3600 seconds")
        self.database = database.expanduser().resolve()
        self.registry = registry
        self.max_depth = max_depth
        self.collect_interval = collect_interval
        self._collect_lock = threading.Lock()
        self._status_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._collecting = False
        self._cycles = 0
        self._last_result: dict[str, object] | None = None
        self._last_error: str | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None

    def collect(self) -> dict[str, object]:
        if not self._collect_lock.acquire(blocking=False):
            raise RuntimeError("collection is already running")
        try:
            with self._status_lock:
                self._collecting = True
                self._last_attempt_at = now()
            roots = self.registry.roots()
            if not roots:
                result: dict[str, object] = {
                    "workspace_count": 0,
                    "inserted_events": 0,
                    "workspaces": [],
                    "discovery_issues": [],
                }
            else:
                with Catalog(self.database) as catalog:
                    result = catalog.collect_roots(roots, max_depth=self.max_depth)
            with self._status_lock:
                self._last_result = result
                self._last_error = None
                self._last_success_at = now()
                self._cycles += 1
            return result
        except Exception as error:
            with self._status_lock:
                self._last_error = str(error)
            raise
        finally:
            with self._status_lock:
                self._collecting = False
            self._collect_lock.release()

    def status(self) -> dict[str, object]:
        with self._status_lock:
            return {
                "enabled": True,
                "collecting": self._collecting,
                "cycles": self._cycles,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "last_result": self._last_result,
                "roots": [str(item) for item in self.registry.roots()],
                "collect_interval": self.collect_interval,
                "max_depth": self.max_depth,
            }

    def dashboard(
        self,
        *,
        workspace: str | None,
        window_hours: int,
        event_limit: int,
    ) -> dict[str, object]:
        with Catalog(self.database) as catalog:
            value = build_dashboard(
                catalog.connection,
                workspace=workspace,
                window_hours=window_hours,
                event_limit=event_limit,
            )
        value["collector"] = self.status()
        return value

    def start(self) -> None:
        if self._thread is not None:
            return
        self.collect()

        def run() -> None:
            while not self._stop.wait(self.collect_interval):
                try:
                    self.collect()
                except Exception:
                    continue

        self._thread = threading.Thread(target=run, name="dev-mesh-collector", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.collect_interval + 1.0))
            self._thread = None
