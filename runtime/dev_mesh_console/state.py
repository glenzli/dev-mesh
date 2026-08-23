"""Serialized collection lifecycle shared by Console API requests and the timer."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from dev_mesh_coord.storage import now
from dev_mesh_observer.catalog import Catalog
from dev_mesh_observer.dashboard import build_dashboard

from .recovery import ReviewedRecovery
from .registry import RootRegistry


class ConsoleState:
    def __init__(
        self,
        *,
        database: Path,
        registry: RootRegistry,
        max_depth: int,
        collect_interval: float,
        discovery_repair: Callable[[], dict[str, object]] | None = None,
    ):
        if max_depth < 0 or max_depth > 12:
            raise ValueError("max depth must be between 0 and 12")
        if collect_interval < 1 or collect_interval > 3600:
            raise ValueError("collect interval must be between 1 and 3600 seconds")
        self.database = database.expanduser().resolve()
        self.registry = registry
        self.max_depth = max_depth
        self.collect_interval = collect_interval
        self._discovery_repair = discovery_repair
        self.recovery = ReviewedRecovery(database=self.database, registry=self.registry)
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

    def preview_run_close(self, *, workspace_id: str, run_id: str) -> dict[str, object]:
        return self.recovery.preview_run_close(workspace_id=workspace_id, run_id=run_id)

    def close_run_after_review(
        self,
        *,
        workspace_id: str,
        run_id: str,
        review_token: str,
        reviewer: str,
        outcome: str,
        reason_code: str,
        evidence: str,
    ) -> dict[str, object]:
        result = self.recovery.close_run(
            workspace_id=workspace_id,
            run_id=run_id,
            review_token=review_token,
            reviewer=reviewer,
            outcome=outcome,
            reason_code=reason_code,
            evidence=evidence,
        )
        try:
            collection = self.collect()
        except Exception as error:
            result["collection"] = {"refreshed": False, "error": str(error)}
        else:
            result["collection"] = {"refreshed": True, "result": collection}
        return result

    def repair_discovery(self) -> dict[str, object]:
        if self._discovery_repair is None:
            raise ValueError("Infra Discovery publication is disabled")
        return self._discovery_repair()

    def set_discovery_repair(self, repair: Callable[[], dict[str, object]]) -> None:
        self._discovery_repair = repair

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
