"""Bounded, redacted Observer facility snapshots for Infra Discovery consumers."""

from __future__ import annotations

import ipaddress
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dev_mesh_coord.control_plane import resolve
from dev_mesh_coord.constants import PROTOCOL_VERSION as COORDINATION_VERSION

from .catalog import Catalog


PROTOCOL_ID = "dev-mesh.observer.status"
PROTOCOL_VERSION = "20260812.1"
REQUEST_SCHEMA = "dev-mesh.observer.status.request"
SNAPSHOT_SCHEMA = "dev-mesh.observer.status.snapshot"
ERROR_SCHEMA = "dev-mesh.observer.status.error"
SERVICE_KIND = "dev-mesh-observer"
SERVICE_INSTANCE_ID = "local"
REQUIRED_REDACTIONS = (
    "branch_names",
    "claim_scopes",
    "coordination_owner_ids",
    "database_paths",
    "event_payloads",
    "git_revisions",
    "raw_errors",
    "workspace_paths",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _loopback_console_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        raise ValueError("facility Console URL must use loopback HTTP(S)")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("facility Console URL must use a literal loopback address") from error
    if not address.is_loopback:
        raise ValueError("facility Console URL must use a literal loopback address")
    return value


def _metric(
    metric_id: str,
    value: object,
    *,
    kind: str = "gauge",
    unit: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"id": metric_id, "kind": kind, "value": value}
    if unit is not None:
        result["unit"] = unit
    return result


def _pending_event_count(
    connection: sqlite3.Connection,
    workspaces: list[dict[str, object]],
) -> int:
    mirrored = {
        str(row[0])
        for row in connection.execute(
            "SELECT source_path FROM events WHERE protocol_version = ?",
            (COORDINATION_VERSION,),
        )
    }
    pending = 0
    for workspace in workspaces:
        if workspace.get("last_error") or workspace.get("not_observed_since"):
            continue
        try:
            plane = resolve(Path(str(workspace["root"])))
            pending += sum(
                str(path) not in mirrored
                for path in (plane.state_root / "events").glob("*.json")
            )
        except (OSError, ValueError):
            continue
    return pending


def build_facility_snapshot(
    *,
    database: Path,
    collector: Mapping[str, object],
    console_url: str,
    service: Mapping[str, str],
    sequence: int,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Project one aggregate snapshot without exposing workspace or Agent identity."""

    if sequence < 1:
        raise ValueError("facility snapshot sequence must be positive")
    now = (captured_at or datetime.now(UTC)).astimezone(UTC)
    with Catalog(database) as catalog:
        report = catalog.report()
        workspaces = list(report["workspaces"])
        pending_events = _pending_event_count(catalog.connection, workspaces)

    registered = int(report["workspace_count"])
    available = sum(
        not item.get("last_error") and not item.get("not_observed_since")
        for item in workspaces
    )
    unavailable = max(0, registered - available)
    integrity_issues = int(report["integrity"]["historical_total"])
    mirrored_events = int(report["event_count"])
    active_contentions = int(report["active"].get("contention", 0))
    stalled_contentions = sum(
        int(count)
        for code, count in report["diagnostic_summary"]["counts"].items()
        if code in {"contention.live-stalled", "contention.orphaned"}
    )

    collector_enabled = bool(collector.get("enabled", True))
    collector_running = bool(collector.get("collecting"))
    collector_cycles = int(collector.get("cycles") or 0)
    collector_interval = float(collector.get("collect_interval") or 0)
    last_success = _parse_time(collector.get("last_success_at"))
    last_attempt = _parse_time(collector.get("last_attempt_at"))
    last_success_age = (
        max(0, int((now - last_success).total_seconds())) if last_success is not None else None
    )
    collector_failed = bool(collector.get("last_error"))
    stale_after = max(60, int(collector_interval * 3))
    collector_stale = bool(
        collector_enabled
        and (
            (last_success_age is not None and last_success_age > stale_after)
            or (
                collector_cycles == 0
                and last_attempt is not None
                and (now - last_attempt).total_seconds() > stale_after
            )
        )
    )
    last_result = collector.get("last_result")
    current_cycle_issues = 0
    if isinstance(last_result, Mapping):
        current_cycle_issues += len(last_result.get("discovery_issues") or [])
        for workspace in last_result.get("workspaces") or []:
            if isinstance(workspace, Mapping):
                current_cycle_issues += int(workspace.get("invalid_count") or 0)

    reason_codes: list[str] = []
    if collector_failed:
        reason_codes.append("collection_failed")
    if collector_stale:
        reason_codes.append("collection_stale")
    if unavailable:
        reason_codes.append("workspace_unavailable")
    if current_cycle_issues:
        reason_codes.append("integrity_issue")
    if stalled_contentions:
        reason_codes.append("contention_stalled")
    if registered == 0:
        reason_codes.append("no_workspaces_registered")

    if collector_enabled and collector_cycles == 0 and not collector_stale:
        state = "starting"
    elif registered > 0 and available == 0:
        state = "unavailable"
    elif reason_codes:
        state = "degraded"
    else:
        state = "healthy"

    captured = _iso(now)
    issues: list[dict[str, object]] = []
    for code, severity, active in (
        ("dev_mesh.collection.failed", "warning", collector_failed),
        ("dev_mesh.collection.stale", "warning", collector_stale),
        ("dev_mesh.workspace.unavailable", "warning", unavailable > 0),
        ("dev_mesh.integrity.issue", "warning", current_cycle_issues > 0),
        ("dev_mesh.contention.stalled", "warning", stalled_contentions > 0),
        ("dev_mesh.collection.backlog", "info", pending_events > 0),
        ("dev_mesh.workspace.none_registered", "info", registered == 0),
    ):
        if active:
            issues.append(
                {
                    "code": code,
                    "severity": severity,
                    "observed_at": captured,
                    "subject_id": "observer",
                }
            )

    metrics = [
        _metric("dev_mesh.workspaces.registered", registered),
        _metric("dev_mesh.workspaces.available", available),
        _metric("dev_mesh.workspaces.unavailable", unavailable),
        _metric("dev_mesh.collection.pending_events", pending_events),
        _metric("dev_mesh.collection.running", collector_running, kind="state"),
        _metric("dev_mesh.integrity.issues", integrity_issues),
        _metric("dev_mesh.events.mirrored", mirrored_events),
        _metric("dev_mesh.contentions.active", active_contentions),
        _metric("dev_mesh.contentions.stalled", stalled_contentions),
    ]
    if last_success_age is not None:
        metrics.append(
            _metric(
                "dev_mesh.collection.last_success_age",
                last_success_age,
                unit="seconds",
            )
        )

    return {
        "schema": SNAPSHOT_SCHEMA,
        "schema_version": PROTOCOL_VERSION,
        "service": {
            "kind": str(service["kind"]),
            "instance_id": str(service["instance_id"]),
            "generation": str(service["generation"]),
        },
        "sequence": sequence,
        "captured_at": captured,
        "status": {"state": state, "reason_codes": reason_codes},
        "headline_metrics": [
            "dev_mesh.workspaces.available",
            "dev_mesh.collection.pending_events",
            "dev_mesh.contentions.stalled",
        ],
        "metrics": metrics,
        "issues": issues,
        "extensions": {"dev-mesh-observer": {"collector_enabled": collector_enabled}},
        "links": {"console_url": _loopback_console_url(console_url)},
        "redaction": {"excluded": list(REQUIRED_REDACTIONS)},
    }


def error_response(code: str) -> dict[str, object]:
    if code not in {"invalid_request", "snapshot_unavailable"}:
        raise ValueError("unsupported facility status error code")
    return {
        "schema": ERROR_SCHEMA,
        "schema_version": PROTOCOL_VERSION,
        "error": {"code": code},
    }
