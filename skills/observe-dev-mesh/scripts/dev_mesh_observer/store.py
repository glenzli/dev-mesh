"""SQLite catalog and idempotent ingestion for immutable coordination events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .catalog import WorkspaceSource, ensure_external_data_dir
from .state_mirror import scan_active_contentions


MAX_EVENT_BYTES = 1024 * 1024
SCHEMA_VERSION = 2


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_data_dir() -> Path:
    configured = os.environ.get("DEV_MESH_OBSERVER_DATA")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "dev-mesh" / "observer"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "dev-mesh" / "observer"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "dev-mesh" / "observer"


SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_roots (
    path TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    coordination_path TEXT NOT NULL UNIQUE,
    workspace_root TEXT NOT NULL,
    git_toplevel TEXT,
    git_common_dir TEXT,
    remote_fingerprint TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_collected_at TEXT,
    available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    source_name TEXT NOT NULL,
    digest TEXT NOT NULL,
    event_at TEXT,
    event_type TEXT,
    run_id TEXT,
    handoff_id TEXT,
    contention_id TEXT,
    request_id TEXT,
    transaction_id TEXT,
    scope TEXT,
    owner TEXT,
    payload_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, source_name)
);

CREATE INDEX IF NOT EXISTS events_at_idx ON events(event_at);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(event_type);
CREATE INDEX IF NOT EXISTS events_run_idx ON events(workspace_id, run_id);
CREATE INDEX IF NOT EXISTS events_handoff_idx ON events(workspace_id, handoff_id);

CREATE TABLE IF NOT EXISTS active_contentions (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    contention_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT,
    coordinator TEXT,
    coordinator_epoch INTEGER,
    lease_until TEXT,
    payload_json TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, contention_id)
);

CREATE INDEX IF NOT EXISTS active_contentions_status_idx
ON active_contentions(status);

CREATE TABLE IF NOT EXISTS collection_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    source_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    expected_digest TEXT,
    observed_digest TEXT NOT NULL,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    UNIQUE (workspace_id, source_name, kind, observed_digest)
);
"""


class ObserverStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.data_dir / "observer.sqlite3"
        self.connection = sqlite3.connect(self.database_path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        existing_version = int(
            self.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if existing_version > SCHEMA_VERSION:
            raise ValueError(
                "Observer database schema is newer than this dev-mesh version: "
                f"{existing_version} > {SCHEMA_VERSION}"
            )
        self.connection.executescript(SCHEMA)
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def __enter__(self) -> "ObserverStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def register_roots(self, roots: Iterable[Path]) -> list[str]:
        timestamp = now()
        normalized = sorted({str(root.expanduser().resolve()) for root in roots})
        with self.connection:
            for path in normalized:
                self.connection.execute(
                    """
                    INSERT INTO scan_roots(path, first_seen_at, last_scanned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET last_scanned_at=excluded.last_scanned_at
                    """,
                    (path, timestamp, timestamp),
                )
        return normalized

    def scan_roots(self) -> list[Path]:
        rows = self.connection.execute("SELECT path FROM scan_roots ORDER BY path")
        return [Path(row["path"]) for row in rows]

    def register_workspaces(
        self,
        sources: Iterable[WorkspaceSource],
    ) -> dict[str, object]:
        source_list = list(sources)
        ensure_external_data_dir(self.data_dir, source_list)
        timestamp = now()
        new_ids: list[str] = []
        existing_ids: list[str] = []
        with self.connection:
            for source in source_list:
                existing = self.connection.execute(
                    "SELECT workspace_id FROM workspaces WHERE coordination_path = ?",
                    (source.coordination_path,),
                ).fetchone()
                if existing is None:
                    workspace_id = str(uuid.uuid4())
                    self.connection.execute(
                        """
                        INSERT INTO workspaces(
                            workspace_id, coordination_path, workspace_root,
                            git_toplevel, git_common_dir, remote_fingerprint,
                            first_seen_at, last_seen_at, available
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            workspace_id,
                            source.coordination_path,
                            source.workspace_root,
                            source.git_toplevel,
                            source.git_common_dir,
                            source.remote_fingerprint,
                            timestamp,
                            timestamp,
                        ),
                    )
                    new_ids.append(workspace_id)
                else:
                    workspace_id = str(existing["workspace_id"])
                    self.connection.execute(
                        """
                        UPDATE workspaces
                        SET workspace_root=?, git_toplevel=?, git_common_dir=?,
                            remote_fingerprint=?, last_seen_at=?, available=1
                        WHERE workspace_id=?
                        """,
                        (
                            source.workspace_root,
                            source.git_toplevel,
                            source.git_common_dir,
                            source.remote_fingerprint,
                            timestamp,
                            workspace_id,
                        ),
                    )
                    existing_ids.append(workspace_id)
        return {
            "discovered": len(new_ids) + len(existing_ids),
            "registered": len(new_ids),
            "existing": len(existing_ids),
            "new_workspace_ids": new_ids,
        }

    def _record_issue(
        self,
        *,
        workspace_id: str,
        source_name: str,
        kind: str,
        detail: str,
        expected_digest: str | None = None,
        observed_digest: str | None = None,
    ) -> None:
        timestamp = now()
        observed_key = observed_digest or ""
        self.connection.execute(
            """
            INSERT INTO collection_issues(
                workspace_id, source_name, kind, detail,
                expected_digest, observed_digest,
                first_detected_at, last_detected_at, occurrences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(workspace_id, source_name, kind, observed_digest)
            DO UPDATE SET
                detail=excluded.detail,
                last_detected_at=excluded.last_detected_at,
                occurrences=collection_issues.occurrences + 1
            """,
            (
                workspace_id,
                source_name,
                kind,
                detail,
                expected_digest,
                observed_key,
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _event_string(record: dict[str, object], key: str) -> str | None:
        value = record.get(key)
        return value if isinstance(value, str) else None

    def _ingest_event(
        self,
        workspace_id: str,
        path: Path,
    ) -> str:
        if path.is_symlink() or not path.is_file():
            self._record_issue(
                workspace_id=workspace_id,
                source_name=path.name,
                kind="unsafe-event-source",
                detail="Event source must be a regular non-symlink file",
            )
            return "issues"
        size = path.stat().st_size
        if size > MAX_EVENT_BYTES:
            self._record_issue(
                workspace_id=workspace_id,
                source_name=path.name,
                kind="event-too-large",
                detail=f"Event is {size} bytes; limit is {MAX_EVENT_BYTES}",
            )
            return "issues"
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        existing = self.connection.execute(
            "SELECT digest FROM events WHERE workspace_id=? AND source_name=?",
            (workspace_id, path.name),
        ).fetchone()
        if existing is not None:
            expected = str(existing["digest"])
            if expected != digest:
                self._record_issue(
                    workspace_id=workspace_id,
                    source_name=path.name,
                    kind="immutable-event-changed",
                    detail="Source event digest changed after its first ingestion",
                    expected_digest=expected,
                    observed_digest=digest,
                )
                return "issues"
            return "skipped"
        try:
            decoded = raw.decode("utf-8")
            record = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._record_issue(
                workspace_id=workspace_id,
                source_name=path.name,
                kind="malformed-event",
                detail=str(error),
                observed_digest=digest,
            )
            return "issues"
        if not isinstance(record, dict):
            self._record_issue(
                workspace_id=workspace_id,
                source_name=path.name,
                kind="malformed-event",
                detail="Event payload must be a JSON object",
                observed_digest=digest,
            )
            return "issues"
        self.connection.execute(
            """
            INSERT INTO events(
                workspace_id, source_name, digest, event_at, event_type,
                run_id, handoff_id, contention_id, request_id, transaction_id,
                scope, owner, payload_json, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                path.name,
                digest,
                self._event_string(record, "at"),
                self._event_string(record, "event"),
                self._event_string(record, "run_id"),
                self._event_string(record, "handoff_id"),
                self._event_string(record, "contention_id"),
                self._event_string(record, "request_id"),
                self._event_string(record, "transaction_id"),
                self._event_string(record, "scope"),
                self._event_string(record, "owner"),
                decoded,
                now(),
            ),
        )
        return "inserted"

    def collect(self) -> dict[str, object]:
        rows = list(
            self.connection.execute(
                "SELECT workspace_id, coordination_path FROM workspaces ORDER BY coordination_path"
            )
        )
        summary: dict[str, int] = {
            "workspaces": len(rows),
            "available": 0,
            "unavailable": 0,
            "seen": 0,
            "inserted": 0,
            "skipped": 0,
            "issues": 0,
            "active_contentions": 0,
            "state_issues": 0,
        }
        timestamp = now()
        with self.connection:
            for row in rows:
                workspace_id = str(row["workspace_id"])
                coordination_path = Path(str(row["coordination_path"]))
                events_path = coordination_path / "events"
                if (
                    coordination_path.is_symlink()
                    or events_path.is_symlink()
                    or not events_path.is_dir()
                ):
                    summary["unavailable"] += 1
                    self.connection.execute(
                        "UPDATE workspaces SET available=0 WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    continue
                summary["available"] += 1
                self.connection.execute(
                    """
                    UPDATE workspaces SET available=1, last_collected_at=?
                    WHERE workspace_id=?
                    """,
                    (timestamp, workspace_id),
                )
                for path in sorted(events_path.glob("*.json")):
                    summary["seen"] += 1
                    outcome = self._ingest_event(workspace_id, path)
                    summary[outcome] += 1
                scan = scan_active_contentions(coordination_path)
                for issue in scan.issues:
                    self._record_issue(
                        workspace_id=workspace_id,
                        source_name=issue.source_name,
                        kind=issue.kind,
                        detail=issue.detail,
                        observed_digest=issue.observed_digest,
                    )
                    summary["issues"] += 1
                    summary["state_issues"] += 1
                if scan.complete:
                    self.connection.execute(
                        "DELETE FROM active_contentions WHERE workspace_id=?",
                        (workspace_id,),
                    )
                    for snapshot in scan.records:
                        record = snapshot.record
                        coordinator = record.get("coordinator")
                        if not isinstance(coordinator, dict):
                            coordinator = {}
                        self.connection.execute(
                            """
                            INSERT INTO active_contentions(
                                workspace_id, contention_id, source_name, digest,
                                status, coordinator, coordinator_epoch, lease_until,
                                payload_json, collected_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                workspace_id,
                                str(record["contention_id"]),
                                snapshot.source_name,
                                snapshot.digest,
                                self._event_string(record, "status"),
                                self._event_string(coordinator, "owner"),
                                coordinator.get("epoch")
                                if isinstance(coordinator.get("epoch"), int)
                                else None,
                                self._event_string(coordinator, "lease_until"),
                                snapshot.payload_json,
                                timestamp,
                            ),
                        )
                    summary["active_contentions"] += len(scan.records)
        return summary

    def status(self) -> dict[str, object]:
        workspaces: list[dict[str, object]] = []
        for row in self.connection.execute(
            """
            SELECT w.*,
                   COUNT(DISTINCT e.source_name) AS event_count,
                   COUNT(DISTINCT i.issue_id) AS issue_count
            FROM workspaces w
            LEFT JOIN events e ON e.workspace_id = w.workspace_id
            LEFT JOIN collection_issues i ON i.workspace_id = w.workspace_id
            GROUP BY w.workspace_id
            ORDER BY w.coordination_path
            """
        ):
            item = dict(row)
            coordination_path = Path(str(item["coordination_path"]))
            events_path = coordination_path / "events"
            item["available"] = bool(
                not coordination_path.is_symlink()
                and not events_path.is_symlink()
                and events_path.is_dir()
            )
            source_event_count = 0
            if item["available"]:
                try:
                    source_event_count = sum(
                        1
                        for path in events_path.glob("*.json")
                        if not path.is_symlink() and path.is_file()
                    )
                except OSError:
                    source_event_count = int(item["event_count"])
            item["source_event_count"] = source_event_count
            item["pending_events"] = max(
                0, source_event_count - int(item["event_count"])
            )
            workspaces.append(item)
        event_count = int(
            self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )
        issue_count = int(
            self.connection.execute("SELECT COUNT(*) FROM collection_issues").fetchone()[0]
        )
        active_contention_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM active_contentions"
            ).fetchone()[0]
        )
        return {
            "data_dir": str(self.data_dir),
            "database": str(self.database_path),
            "summary": {
                "workspaces": len(workspaces),
                "available": sum(bool(item["available"]) for item in workspaces),
                "events": event_count,
                "issues": issue_count,
                "pending_events": sum(
                    int(item["pending_events"]) for item in workspaces
                ),
                "active_contentions": active_contention_count,
            },
            "scan_roots": [str(path) for path in self.scan_roots()],
            "workspaces": workspaces,
        }
