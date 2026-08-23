"""Bounded SQLite catalog built only from immutable events and current snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from dev_mesh_coord.constants import PROTOCOL, PROTOCOL_VERSION
from dev_mesh_coord.errors import ProtocolError
from dev_mesh_coord.storage import now

from .reports import build_report
from .source_plane import SourcePlaneError, resolve_source_plane
from .source_validation import event_record, event_source, snapshot_record


SKIP_DIRECTORIES = {".git", ".dev-mesh", ".agent-coordination", "node_modules", "target", "dist"}
MAX_INVALID_RECORDS = 20
SNAPSHOT_DIRECTORIES = (
    ("run", "current", "runs"),
    ("claim", "current", "claims"),
    ("claim", "archive", "archive/claims"),
    ("handoff", "current", "handoffs"),
    ("contention", "active", "contentions/active"),
    ("contention", "archive", "contentions/archive"),
    ("transaction", "active", "transactions/active"),
    ("transaction", "archive", "transactions/archive"),
    ("direct-commit", "active", "direct-commits/active"),
    ("direct-commit", "archive", "direct-commits/archive"),
    ("cleanup", "active", "cleanups/active"),
    ("cleanup", "archive", "cleanups/archive"),
    ("work", "active", "work/active"),
    ("work", "archive", "work/archive"),
    ("work-result", "current", "work-results"),
)


def workspace_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:24]


def discover_with_issues(
    roots: Iterable[Path], *, max_depth: int = 5
) -> tuple[list[Path], list[dict[str, object]]]:
    discovered: set[Path] = set()
    issues: list[dict[str, object]] = []
    for candidate in roots:
        candidate = candidate.expanduser().resolve()
        if not candidate.is_dir():
            continue
        for current, directories, _files in os.walk(candidate, followlinks=False):
            path = Path(current)
            depth = len(path.relative_to(candidate).parts)
            directories[:] = [
                item
                for item in directories
                if item not in SKIP_DIRECTORIES and not (path / item).is_symlink() and depth < max_depth
            ]
            marker = path / ".dev-mesh" / "manifest.json"
            if marker.is_file() and not marker.is_symlink():
                try:
                    plane = resolve_source_plane(path)
                except (ProtocolError, SourcePlaneError, OSError) as error:
                    issues.append(
                        {
                            "root": str(path.resolve()),
                            "code": getattr(error, "code", "workspace.unavailable"),
                            "message": str(error),
                            "source_protocol_version": getattr(
                                error, "source_protocol_version", None
                            ),
                        }
                    )
                    continue
                discovered.add(path.resolve())
                directories[:] = []
    return sorted(discovered), issues


def discover_workspaces(roots: Iterable[Path], *, max_depth: int = 5) -> list[Path]:
    return discover_with_issues(roots, max_depth=max_depth)[0]


def _object_id(kind: str, record: dict[str, object], path: Path) -> str:
    fields = {
        "run": "run_id",
        "claim": "scope",
        "handoff": "handoff_id",
        "contention": "contention_id",
        "transaction": "transaction_id",
        "direct-commit": "direct_commit_id",
        "cleanup": "cleanup_id",
        "work": "work_state_id",
        "work-result": "result_id",
    }
    value = record.get(fields[kind])
    return str(value) if isinstance(value, str) else path.stem


class Catalog:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        if ".dev-mesh" in self.path.parts or ".agent-coordination" in self.path.parts:
            raise ValueError("Observer catalog must remain outside coordination authority state")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                root TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                last_error TEXT,
                last_seen_scan TEXT,
                not_observed_since TEXT,
                source_protocol_version TEXT,
                issue_code TEXT,
                PRIMARY KEY (workspace_id, protocol_version)
            );
            CREATE TABLE IF NOT EXISTS events (
                workspace_id TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                event_id TEXT NOT NULL,
                at TEXT NOT NULL,
                event TEXT NOT NULL,
                authority_effect TEXT NOT NULL,
                owner TEXT,
                run_id TEXT,
                scope TEXT,
                transaction_id TEXT,
                contention_id TEXT,
                handoff_id TEXT,
                record_json TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_sha256 TEXT,
                PRIMARY KEY (workspace_id, protocol_version, event_id)
            );
            CREATE INDEX IF NOT EXISTS events_workspace_time
                ON events(workspace_id, protocol_version, at);
            CREATE INDEX IF NOT EXISTS events_owner_run
                ON events(owner, run_id, at);
            CREATE TABLE IF NOT EXISTS integrity_findings (
                workspace_id TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                code TEXT NOT NULL,
                object_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                expected_sha256 TEXT NOT NULL,
                observed_sha256 TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                last_observed_at TEXT,
                resolved_at TEXT,
                PRIMARY KEY (
                    workspace_id, protocol_version, code, object_id, observed_sha256
                )
            );
            """
        )
        workspace_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(workspaces)")
        }
        if "last_seen_scan" not in workspace_columns:
            self.connection.execute("ALTER TABLE workspaces ADD COLUMN last_seen_scan TEXT")
        if "not_observed_since" not in workspace_columns:
            self.connection.execute("ALTER TABLE workspaces ADD COLUMN not_observed_since TEXT")
        if "source_protocol_version" not in workspace_columns:
            self.connection.execute(
                "ALTER TABLE workspaces ADD COLUMN source_protocol_version TEXT"
            )
        if "issue_code" not in workspace_columns:
            self.connection.execute("ALTER TABLE workspaces ADD COLUMN issue_code TEXT")
        finding_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(integrity_findings)")
        }
        if "last_observed_at" not in finding_columns:
            self.connection.execute("ALTER TABLE integrity_findings ADD COLUMN last_observed_at TEXT")
        if "resolved_at" not in finding_columns:
            self.connection.execute("ALTER TABLE integrity_findings ADD COLUMN resolved_at TEXT")
        event_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(events)")
        }
        if "source_sha256" not in event_columns:
            # The candidate has not been published. Preserve collected events while
            # establishing an exact-byte baseline on their next source observation.
            self.connection.execute("ALTER TABLE events ADD COLUMN source_sha256 TEXT")

        snapshot_table = list(self.connection.execute("PRAGMA table_info(snapshots)"))
        snapshot_columns = {str(row[1]) for row in snapshot_table}
        snapshot_primary_key = tuple(
            str(row[1]) for row in sorted(snapshot_table, key=lambda row: int(row[5])) if row[5]
        )
        expected_snapshot_primary_key = (
            "workspace_id",
            "protocol_version",
            "kind",
            "object_id",
            "lifecycle",
            "source_path",
        )
        if snapshot_columns and (
            not {"lifecycle", "source_path"}.issubset(snapshot_columns)
            or snapshot_primary_key != expected_snapshot_primary_key
        ):
            # Snapshots are a replaceable projection. Rebuild the unpublished shape
            # instead of collapsing distinct archived instances of a reused semantic id.
            self.connection.execute("DROP TABLE snapshots")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                workspace_id TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                kind TEXT NOT NULL,
                object_id TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                status TEXT,
                record_json TEXT NOT NULL,
                source_path TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (
                    workspace_id, protocol_version, kind, object_id, lifecycle, source_path
                )
            )
            """
        )
        self.connection.commit()

    def _record_integrity_finding(
        self,
        *,
        identifier: str,
        code: str,
        object_id: str,
        source_path: str,
        expected_sha256: str,
        observed_sha256: str,
        observed_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO integrity_findings (
                workspace_id, protocol_version, code, object_id, source_path,
                expected_sha256, observed_sha256, observed_at, last_observed_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(workspace_id, protocol_version, code, object_id, observed_sha256)
            DO UPDATE SET
                source_path = excluded.source_path,
                expected_sha256 = excluded.expected_sha256,
                last_observed_at = excluded.last_observed_at,
                resolved_at = NULL
            """,
            (
                identifier,
                PROTOCOL_VERSION,
                code,
                object_id,
                source_path,
                expected_sha256,
                observed_sha256,
                observed_at,
                observed_at,
            ),
        )

    def collect_workspace(self, root: Path, *, scan_id: str | None = None) -> dict[str, object]:
        plane = resolve_source_plane(root)
        identifier = workspace_id(plane.workspace_root)
        inserted = 0
        invalid: list[str] = []
        invalid_count = 0
        collected_at = now()

        def record_invalid(message: str) -> None:
            nonlocal invalid_count
            invalid_count += 1
            if len(invalid) < MAX_INVALID_RECORDS:
                invalid.append(message)

        with self.connection:
            existing_workspace = self.connection.execute(
                """
                SELECT source_protocol_version FROM workspaces
                WHERE workspace_id = ? AND protocol_version = ?
                """,
                (identifier, PROTOCOL_VERSION),
            ).fetchone()
            previous_source_version = (
                str(existing_workspace["source_protocol_version"])
                if existing_workspace is not None
                and existing_workspace["source_protocol_version"] is not None
                else None
            )
            if previous_source_version is not None and previous_source_version != plane.version:
                # A source cutover replaces the observed authority namespace. Do not
                # reinterpret the retired source as missing or mutated current evidence.
                for table in ("events", "snapshots", "integrity_findings"):
                    self.connection.execute(
                        f"DELETE FROM {table} WHERE workspace_id = ? AND protocol_version = ?",
                        (identifier, PROTOCOL_VERSION),
                    )
            # A first-seen invalid source is a current collection condition, not an
            # immutable authority fact. Preserve its row but resolve it unless this
            # scan observes the same violation again.
            self.connection.execute(
                """
                UPDATE integrity_findings
                SET resolved_at = COALESCE(resolved_at, ?)
                WHERE workspace_id = ? AND protocol_version = ?
                  AND code = 'event.source-invalid' AND resolved_at IS NULL
                """,
                (collected_at, identifier, PROTOCOL_VERSION),
            )
            event_paths = sorted((plane.state_root / "events").glob("*.json"))
            seen_source_paths = {str(path) for path in event_paths}
            for path in event_paths:
                source_existing = self.connection.execute(
                    """
                    SELECT event_id, source_sha256
                    FROM events
                    WHERE workspace_id = ? AND protocol_version = ? AND source_path = ?
                    """,
                    (identifier, PROTOCOL_VERSION, str(path)),
                ).fetchone()
                try:
                    encoded, source_sha256 = event_source(path, plane.state_root)
                except (ProtocolError, OSError) as error:
                    if source_existing is None:
                        code = "event.source-invalid"
                        object_id = (
                            "source-"
                            + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
                        )
                        expected = "readable-valid-event-source"
                    else:
                        code = "event.source-mutated"
                        object_id = str(source_existing["event_id"])
                        expected = str(
                            source_existing["source_sha256"] or "previously-collected-event"
                        )
                    self._record_integrity_finding(
                        identifier=identifier,
                        code=code,
                        object_id=object_id,
                        source_path=str(path),
                        expected_sha256=expected,
                        observed_sha256="unreadable-" + hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
                        observed_at=now(),
                    )
                    record_invalid(f"{path.name}: {error}")
                    continue
                if source_existing is not None:
                    expected_sha256 = source_existing["source_sha256"]
                    if expected_sha256 is None:
                        self.connection.execute(
                            """
                            UPDATE events SET source_sha256 = ?
                            WHERE workspace_id = ? AND protocol_version = ? AND source_path = ?
                            """,
                            (source_sha256, identifier, PROTOCOL_VERSION, str(path)),
                        )
                    elif str(expected_sha256) != source_sha256:
                        self._record_integrity_finding(
                            identifier=identifier,
                            code="event.source-mutated",
                            object_id=str(source_existing["event_id"]),
                            source_path=str(path),
                            expected_sha256=str(expected_sha256),
                            observed_sha256=source_sha256,
                            observed_at=now(),
                        )
                        record_invalid(f"{path.name}: immutable event source changed")
                    continue
                try:
                    record = event_record(
                        path,
                        encoded,
                        protocol_version=plane.version,
                        event_schema=plane.event_schema,
                    )
                except ProtocolError as error:
                    object_id = "source-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
                    self._record_integrity_finding(
                        identifier=identifier,
                        code="event.source-invalid",
                        object_id=object_id,
                        source_path=str(path),
                        expected_sha256="valid-event-envelope",
                        observed_sha256=source_sha256,
                        observed_at=now(),
                    )
                    record_invalid(f"{path.name}: {error}")
                    continue
                event_id = str(record["event_id"])
                existing = self.connection.execute(
                    """
                    SELECT source_sha256, source_path
                    FROM events
                    WHERE workspace_id = ? AND protocol_version = ? AND event_id = ?
                    """,
                    (identifier, PROTOCOL_VERSION, event_id),
                ).fetchone()
                if existing is not None:
                    expected_sha256 = existing["source_sha256"]
                    if expected_sha256 is None:
                        self.connection.execute(
                            """
                            UPDATE events SET source_sha256 = ?
                            WHERE workspace_id = ? AND protocol_version = ? AND event_id = ?
                            """,
                            (source_sha256, identifier, PROTOCOL_VERSION, event_id),
                        )
                    elif str(expected_sha256) != source_sha256:
                        self._record_integrity_finding(
                            identifier=identifier,
                            code="event.source-mutated",
                            object_id=event_id,
                            source_path=str(path),
                            expected_sha256=str(expected_sha256),
                            observed_sha256=source_sha256,
                            observed_at=now(),
                        )
                        record_invalid(f"{path.name}: immutable event source changed")
                    continue
                cursor = self.connection.execute(
                    """
                    INSERT INTO events (
                        workspace_id, protocol_version, event_id, at, event,
                        authority_effect, owner, run_id, scope, transaction_id,
                        contention_id, handoff_id, record_json, source_path, source_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        PROTOCOL_VERSION,
                        event_id,
                        record["at"],
                        record["event"],
                        record["authority_effect"],
                        record.get("owner"),
                        record.get("run_id"),
                        record.get("scope"),
                        record.get("transaction_id"),
                        record.get("contention_id"),
                        record.get("handoff_id"),
                        json.dumps(record, ensure_ascii=False, sort_keys=True),
                        str(path),
                        source_sha256,
                    ),
                )
                inserted += cursor.rowcount

            for source in self.connection.execute(
                """
                SELECT event_id, source_path, source_sha256
                FROM events
                WHERE workspace_id = ? AND protocol_version = ?
                """,
                (identifier, PROTOCOL_VERSION),
            ):
                if str(source["source_path"]) in seen_source_paths:
                    continue
                self._record_integrity_finding(
                    identifier=identifier,
                    code="event.source-missing",
                    object_id=str(source["event_id"]),
                    source_path=str(source["source_path"]),
                    expected_sha256=str(source["source_sha256"] or "previously-collected-event"),
                    observed_sha256="missing",
                    observed_at=now(),
                )

            self.connection.execute(
                "DELETE FROM snapshots WHERE workspace_id = ? AND protocol_version = ?",
                (identifier, PROTOCOL_VERSION),
            )
            observed_at = collected_at
            snapshot_count = 0
            for kind, lifecycle, relative in SNAPSHOT_DIRECTORIES:
                directory = plane.state_root / relative
                for path in sorted(directory.glob("*.json")):
                    try:
                        record = snapshot_record(
                            path,
                            plane.state_root,
                            kind,
                            lifecycle,
                            protocol_version=plane.version,
                        )
                    except (ProtocolError, OSError) as error:
                        record_invalid(f"{path.relative_to(plane.state_root)}: {error}")
                        continue
                    self.connection.execute(
                        """
                        INSERT INTO snapshots (
                            workspace_id, protocol_version, kind, object_id,
                            lifecycle, status, record_json, source_path, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identifier,
                            PROTOCOL_VERSION,
                            kind,
                            _object_id(kind, record, path),
                            lifecycle,
                            record.get("status"),
                            json.dumps(record, ensure_ascii=False, sort_keys=True),
                            str(path),
                            observed_at,
                        ),
                    )
                    snapshot_count += 1
            self.connection.execute(
                """
                INSERT INTO workspaces (
                    workspace_id, protocol_version, root, last_collected_at, last_error,
                    last_seen_scan, not_observed_since, source_protocol_version, issue_code
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)
                ON CONFLICT(workspace_id, protocol_version) DO UPDATE SET
                    root = excluded.root,
                    last_collected_at = excluded.last_collected_at,
                    last_error = excluded.last_error,
                    last_seen_scan = COALESCE(excluded.last_seen_scan, workspaces.last_seen_scan),
                    not_observed_since = NULL,
                    source_protocol_version = excluded.source_protocol_version,
                    issue_code = NULL
                """,
                (
                    identifier,
                    PROTOCOL_VERSION,
                    str(plane.workspace_root),
                    observed_at,
                    "\n".join(invalid) if invalid else None,
                    scan_id,
                    plane.version,
                ),
            )
        return {
            "workspace_id": identifier,
            "root": str(plane.workspace_root),
            "source_protocol_version": plane.version,
            "inserted_events": inserted,
            "snapshots": snapshot_count,
            "invalid_records": invalid,
            "invalid_count": invalid_count,
            "invalid_records_truncated": invalid_count > len(invalid),
        }

    def collect_roots(self, roots: Iterable[Path], *, max_depth: int = 5) -> dict[str, object]:
        scan_roots = [candidate.expanduser().resolve() for candidate in roots]
        scan_at = now()
        scan_id = f"{scan_at}-{uuid.uuid4()}"
        discovered, discovery_issues = discover_with_issues(scan_roots, max_depth=max_depth)
        results = [self.collect_workspace(root, scan_id=scan_id) for root in discovered]
        with self.connection:
            for issue in discovery_issues:
                issue_root = str(issue["root"])
                identifier = workspace_id(Path(issue_root))
                source_protocol_version = issue.get("source_protocol_version")
                previous = self.connection.execute(
                    """
                    SELECT source_protocol_version FROM workspaces
                    WHERE workspace_id = ? AND protocol_version = ?
                    """,
                    (identifier, PROTOCOL_VERSION),
                ).fetchone()
                if (
                    source_protocol_version is not None
                    and previous is not None
                    and previous["source_protocol_version"] != source_protocol_version
                ):
                    for table in ("events", "snapshots", "integrity_findings"):
                        self.connection.execute(
                            f"DELETE FROM {table} WHERE workspace_id = ? AND protocol_version = ?",
                            (identifier, PROTOCOL_VERSION),
                        )
                self.connection.execute(
                    """
                    INSERT INTO workspaces (
                        workspace_id, protocol_version, root, last_collected_at, last_error,
                        last_seen_scan, not_observed_since, source_protocol_version, issue_code
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(workspace_id, protocol_version) DO UPDATE SET
                        root = excluded.root,
                        last_collected_at = excluded.last_collected_at,
                        last_error = excluded.last_error,
                        last_seen_scan = excluded.last_seen_scan,
                        not_observed_since = NULL,
                        source_protocol_version = excluded.source_protocol_version,
                        issue_code = excluded.issue_code
                    """,
                    (
                        identifier,
                        PROTOCOL_VERSION,
                        issue_root,
                        scan_at,
                        f"{issue['code']}: {issue['message']}",
                        scan_id,
                        source_protocol_version,
                        issue["code"],
                    ),
                )
            known = list(
                self.connection.execute(
                    "SELECT workspace_id, root, last_seen_scan FROM workspaces WHERE protocol_version = ?",
                    (PROTOCOL_VERSION,),
                )
            )
            for item in known:
                known_root = Path(str(item["root"])).expanduser().resolve()
                in_scan = False
                for scan_root in scan_roots:
                    try:
                        known_root.relative_to(scan_root)
                    except ValueError:
                        continue
                    in_scan = True
                    break
                if in_scan and item["last_seen_scan"] != scan_id:
                    self.connection.execute(
                        """
                        UPDATE workspaces
                        SET not_observed_since = COALESCE(not_observed_since, ?)
                        WHERE workspace_id = ? AND protocol_version = ?
                        """,
                        (scan_at, item["workspace_id"], PROTOCOL_VERSION),
                    )
        return {
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "scan_id": scan_id,
            "scan_at": scan_at,
            "workspaces": results,
            "workspace_count": len(results),
            "inserted_events": sum(int(item["inserted_events"]) for item in results),
            "discovery_issues": discovery_issues,
        }

    def report(self, *, workspace: str | None = None, stale_after_seconds: int = 1800) -> dict[str, object]:
        return build_report(
            self.connection,
            workspace=workspace,
            stale_after_seconds=stale_after_seconds,
        )
