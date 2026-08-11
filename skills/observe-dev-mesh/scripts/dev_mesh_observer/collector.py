"""Serialized background collection lifecycle for the localhost console."""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .operations import collect_registered, discover_and_collect
from .store import ObserverStore


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class LiveCollector:
    """Own one coalesced collection slot, timer, result, and shutdown boundary."""

    def __init__(
        self,
        *,
        data_dir: Path,
        max_depth: int,
        interval_seconds: float,
    ) -> None:
        if interval_seconds < 0 or interval_seconds > 3600:
            raise ValueError("collect interval must be between 0 and 3600 seconds")
        self.data_dir = data_dir
        self.max_depth = max_depth
        self.interval_seconds = float(interval_seconds)
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, object] = {
            "enabled": self.interval_seconds > 0,
            "interval_seconds": self.interval_seconds,
            "running": False,
            "cycles": 0,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "last_result": None,
        }

    def start(self) -> None:
        if self.interval_seconds <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="dev-mesh-observer-collector",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None

    def _execute(
        self,
        operation: Callable[[ObserverStore], dict[str, object]],
    ) -> dict[str, object]:
        with self._operation_lock:
            with self._state_lock:
                self._state["running"] = True
                self._state["last_attempt_at"] = _now()
            try:
                with ObserverStore(self.data_dir) as store:
                    result = operation(store)
            except Exception as error:
                with self._state_lock:
                    self._state["running"] = False
                    self._state["last_error_at"] = _now()
                    self._state["last_error"] = str(error)[:500]
                raise
            with self._state_lock:
                self._state["running"] = False
                self._state["cycles"] = int(self._state["cycles"]) + 1
                self._state["last_success_at"] = _now()
                self._state["last_error"] = None
                self._state["last_result"] = result
            return result

    def collect_now(self) -> dict[str, object]:
        return self._execute(
            lambda store: collect_registered(store, max_depth=self.max_depth)
        )

    def add_workspace(self, root: Path, *, max_depth: int) -> dict[str, object]:
        return self._execute(
            lambda store: discover_and_collect(
                store,
                roots=[root],
                max_depth=max_depth,
            )
        )

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_now()
            except Exception as error:  # pragma: no cover - diagnostic logging.
                print(f"observer collector: {error}", file=sys.stderr)
            if self._stop.wait(self.interval_seconds):
                break

    def status(self) -> dict[str, object]:
        with self._state_lock:
            return dict(self._state)
