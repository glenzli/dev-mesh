"""Crash-retryable cutover from the retired coordination state version."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from . import git_backend as git
from .analysis_retention import RETENTION_POLICY, build_analysis_retention
from .constants import EVENT_SCHEMA, PROTOCOL, PROTOCOL_VERSION, STATE_DIRECTORIES
from .cutover import git_facts, tree_digest
from .errors import ProtocolError
from .storage import (
    advisory_lock,
    ensure_regular_directory,
    now,
    read_json,
    replace_json,
    require_identifier,
)


SOURCE_VERSION = "20260814.1"
SOURCE_EVENT_SCHEMA = 2


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _paths(
    root: Path, cutover_id: str
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    coord = root / ".dev-mesh" / "coord"
    source = coord / SOURCE_VERSION
    target = coord / PROTOCOL_VERSION
    retention = coord / "analysis" / cutover_id / f"{SOURCE_VERSION}.json"
    discard_staging = coord / "cutovers" / f".{cutover_id}.{SOURCE_VERSION}.discarding"
    prior_archives = coord / "archive"
    prior_archives_staging = coord / "cutovers" / f".{cutover_id}.prior-archives.discarding"
    journal = coord / "cutovers" / f"{cutover_id}.json"
    return (
        source,
        target,
        retention,
        discard_staging,
        prior_archives,
        prior_archives_staging,
        journal,
    )


def _exact_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir() or git.repository_root(resolved) != resolved:
        raise ValueError("version cutover root must be the exact Git workspace root")
    return resolved


def _cutover_id(value: str) -> str:
    return require_identifier(value, "cutover id")


def _assert_owned_paths(root: Path, *, cutover_id: str) -> None:
    coord = root / ".dev-mesh" / "coord"
    ensure_regular_directory(root / ".dev-mesh", "Dev Mesh namespace")
    ensure_regular_directory(coord, "coordination namespace")
    for parent in (
        coord / "analysis",
        coord / "analysis" / cutover_id,
        coord / "archive",
        coord / "cutovers",
    ):
        if parent.exists() or parent.is_symlink():
            ensure_regular_directory(parent, "version cutover state")
    discard_staging = coord / "cutovers" / f".{cutover_id}.{SOURCE_VERSION}.discarding"
    prior_archives_staging = coord / "cutovers" / f".{cutover_id}.prior-archives.discarding"
    for staging in (discard_staging, prior_archives_staging):
        if staging.exists() or staging.is_symlink():
            ensure_regular_directory(staging, "retired source staging")


def _authority_inventory(state: Path) -> dict[str, int]:
    def unresolved(directory: Path, *, terminal_statuses: set[str] | None = None) -> int:
        count = 0
        for path in directory.glob("*.json") if directory.is_dir() else []:
            if terminal_statuses is None:
                count += 1
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                count += 1
                continue
            status = value.get("status") if isinstance(value, dict) else None
            if status not in terminal_statuses:
                count += 1
        return count

    return {
        "runs": unresolved(state / "runs", terminal_statuses={"closed"}),
        "claims": unresolved(state / "claims"),
        "handoffs": unresolved(
            state / "handoffs",
            terminal_statuses={"accepted", "rejected", "withdrawn"},
        ),
        "contentions": unresolved(state / "contentions" / "active"),
        "work": unresolved(state / "work" / "active"),
        "transactions": unresolved(state / "transactions" / "active"),
        "direct_commits": unresolved(state / "direct-commits" / "active"),
        "cleanups": unresolved(state / "cleanups" / "active"),
    }


def _raw_current(root: Path) -> dict[str, object]:
    current = read_json(root / ".dev-mesh" / "coord" / "current.json")
    if current.get("protocol") != PROTOCOL:
        raise ProtocolError("unsupported_protocol", "current marker names another protocol")
    return current


def build_plan(root: Path, *, cutover_id: str) -> dict[str, object]:
    cutover_id = _cutover_id(cutover_id)
    root = _exact_root(root)
    (
        source,
        target,
        retention,
        discard_staging,
        prior_archives,
        prior_archives_staging,
        journal,
    ) = _paths(root, cutover_id)
    with advisory_lock(root / ".dev-mesh.bootstrap.lock"):
        _assert_owned_paths(root, cutover_id=cutover_id)
        current = _raw_current(root)
        if current.get("version") != SOURCE_VERSION or current.get("event_schema") != SOURCE_EVENT_SCHEMA:
            raise ProtocolError("unsupported_protocol", "version cutover requires the exact retired source version")
        if (
            target.exists()
            or retention.exists()
            or discard_staging.exists()
            or prior_archives_staging.exists()
            or journal.exists()
        ):
            raise ProtocolError("already_exists", "version cutover id or target already exists")
        source_digest = tree_digest(source)
        planned_at = now()
        retained = build_analysis_retention(
            source,
            source_version=SOURCE_VERSION,
            source_event_schema=SOURCE_EVENT_SCHEMA,
            source_state_sha256=source_digest,
            recorded_at=planned_at,
        )
        retention_summary = {
            "policy": RETENTION_POLICY,
            "path": retention.relative_to(root / ".dev-mesh").as_posix(),
            "record_sha256": _digest(retained),
            "total_event_count": retained["total_event_count"],
            "retained_event_count": retained["retained_event_count"],
            "omitted_event_count": retained["omitted_event_count"],
            "events_truncated": retained["events_truncated"],
        }
        plan: dict[str, object] = {
            "schema": 1,
            "kind": "dev-mesh.coordination.version-cutover",
            "cutover_id": cutover_id,
            "protocol": PROTOCOL,
            "source_version": SOURCE_VERSION,
            "source_event_schema": SOURCE_EVENT_SCHEMA,
            "target_version": PROTOCOL_VERSION,
            "target_event_schema": EVENT_SCHEMA,
            "workspace_root": str(root),
            "source_state_sha256": source_digest,
            "authority_inventory": _authority_inventory(source),
            "source_disposition": "discard-after-analysis-retention",
            "analysis_retention": retention_summary,
            "prior_archives": {
                "present": prior_archives.is_dir() and not prior_archives.is_symlink(),
                "tree_sha256": (
                    tree_digest(prior_archives)
                    if prior_archives.is_dir() and not prior_archives.is_symlink()
                    else None
                ),
                "disposition": "discard",
            },
            "git_facts": git_facts(root),
            "planned_at": planned_at,
        }
        plan["plan_digest"] = _digest(plan)
        journal.parent.mkdir(parents=True, exist_ok=True)
        replace_json(journal, {**plan, "state": "planned"}, base=root / ".dev-mesh")
        return plan


def _load_plan(root: Path, cutover_id: str, expected_digest: str) -> tuple[Path, dict[str, object]]:
    (
        _source,
        _target,
        _retention,
        _discard_staging,
        _prior_archives,
        _prior_archives_staging,
        journal,
    ) = _paths(root, cutover_id)
    record = read_json(journal, base=root / ".dev-mesh")
    state = record.pop("state", None)
    transitions = {key: record.pop(key) for key in list(record) if key.endswith("_at") and key != "planned_at"}
    digest = record.pop("plan_digest", None)
    if digest != expected_digest or _digest(record) != expected_digest:
        raise ProtocolError("cutover_facts_changed", "version cutover plan digest changed")
    record.update({"plan_digest": digest, "state": state, **transitions})
    return journal, record


def _transition(root: Path, journal: Path, record: dict[str, object], state: str) -> dict[str, object]:
    updated = {**record, "state": state, f"{state.replace('-', '_')}_at": now()}
    replace_json(journal, updated, base=root / ".dev-mesh")
    return updated


def apply(
    root: Path,
    *,
    cutover_id: str,
    expected_plan_digest: str,
    confirm_agents_stopped: bool,
    confirm_discard_old_state: bool,
) -> dict[str, object]:
    if not confirm_agents_stopped:
        raise ProtocolError("cutover_confirmation_required", "confirm that all old writers stopped")
    cutover_id = _cutover_id(cutover_id)
    root = _exact_root(root)
    (
        source,
        target,
        retention,
        discard_staging,
        prior_archives,
        prior_archives_staging,
        _journal_path,
    ) = _paths(root, cutover_id)
    with advisory_lock(root / ".dev-mesh.bootstrap.lock"):
        _assert_owned_paths(root, cutover_id=cutover_id)
        journal, record = _load_plan(root, cutover_id, expected_plan_digest)
        if not confirm_discard_old_state:
            raise ProtocolError(
                "cutover_confirmation_required",
                "explicit old-state discard confirmation is required",
            )
        if git_facts(root) != record.get("git_facts"):
            raise ProtocolError("cutover_facts_changed", "Git or dirty baseline changed after review")
        expected_source_digest = str(record["source_state_sha256"])
        retained_source: Path | None = None
        if source.exists():
            retained_source = source
        elif discard_staging.exists():
            retained_source = discard_staging
        if retained_source is not None:
            if tree_digest(retained_source) != expected_source_digest:
                raise ProtocolError("cutover_facts_changed", "retired state changed after review")
            retained = build_analysis_retention(
                retained_source,
                source_version=SOURCE_VERSION,
                source_event_schema=SOURCE_EVENT_SCHEMA,
                source_state_sha256=expected_source_digest,
                recorded_at=str(record["planned_at"]),
            )
            retention_summary = record.get("analysis_retention")
            if (
                not isinstance(retention_summary, dict)
                or _digest(retained) != retention_summary.get("record_sha256")
            ):
                raise ProtocolError("cutover_facts_changed", "analysis retention facts changed")
            retention.parent.mkdir(parents=True, exist_ok=True)
            if retention.exists():
                if read_json(retention, base=root / ".dev-mesh") != retained:
                    raise ProtocolError("cutover_facts_changed", "analysis retention record changed")
            else:
                replace_json(retention, retained, base=root / ".dev-mesh")
            if source.exists():
                os.replace(source, discard_staging)
            record = _transition(root, journal, record, "source-retained")
        else:
            if record.get("state") not in {"ready-to-discard", "completed"}:
                raise ProtocolError(
                    "cutover_facts_changed",
                    "retired state disappeared before its controlled discard point",
                )
            retention_summary = record.get("analysis_retention")
            if (
                not isinstance(retention_summary, dict)
                or not retention.is_file()
                or retention.is_symlink()
                or _digest(read_json(retention, base=root / ".dev-mesh"))
                != retention_summary.get("record_sha256")
            ):
                raise ProtocolError("cutover_facts_changed", "analysis retention record changed")

        prior_archive_facts = record.get("prior_archives")
        if not isinstance(prior_archive_facts, dict):
            raise ProtocolError("cutover_facts_changed", "prior archive facts are malformed")
        prior_archive_source: Path | None = None
        if prior_archives.exists() or prior_archives.is_symlink():
            if prior_archives.is_symlink() or not prior_archives.is_dir():
                raise ProtocolError("cutover_facts_changed", "prior archive root is unsafe")
            prior_archive_source = prior_archives
        elif prior_archives_staging.exists() or prior_archives_staging.is_symlink():
            if prior_archives_staging.is_symlink() or not prior_archives_staging.is_dir():
                raise ProtocolError("cutover_facts_changed", "prior archive staging is unsafe")
            prior_archive_source = prior_archives_staging
        if prior_archive_source is not None:
            if not prior_archive_facts.get("present"):
                raise ProtocolError("cutover_facts_changed", "unreviewed prior archives appeared")
            if tree_digest(prior_archive_source) != prior_archive_facts.get("tree_sha256"):
                raise ProtocolError("cutover_facts_changed", "prior archive tree changed")
            if prior_archives.exists():
                os.replace(prior_archives, prior_archives_staging)
        elif prior_archive_facts.get("present") and record.get("state") not in {
            "ready-to-discard",
            "completed",
        }:
            raise ProtocolError("cutover_facts_changed", "prior archives disappeared early")

        activated_at = str(record.get("target_created_at") or now())
        if "target_created_at" not in record:
            record = {**record, "target_created_at": activated_at}
            replace_json(journal, record, base=root / ".dev-mesh")
        target.mkdir(parents=True, exist_ok=True)
        for relative in STATE_DIRECTORIES:
            (target / relative).mkdir(parents=True, exist_ok=True)
        protocol_record = {
            "schema": 1,
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "event_schema": EVENT_SCHEMA,
            "created_at": activated_at,
            "cutover_id": cutover_id,
        }
        protocol_path = target / "protocol.json"
        if protocol_path.exists():
            if read_json(protocol_path, base=target) != protocol_record:
                raise ProtocolError("cutover_facts_changed", "target protocol facts changed")
        else:
            replace_json(protocol_path, protocol_record, base=target)
        baseline = {
            "schema": 1,
            "kind": "dev-mesh.coordination.cutover-baseline",
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "cutover_id": cutover_id,
            "source_version": SOURCE_VERSION,
            "source_state_sha256": expected_source_digest,
            "source_disposition": "discarded-after-analysis-retention",
            "analysis_retention": record["analysis_retention"],
            "prior_archives_disposition": "discarded",
            "git_facts": record["git_facts"],
            "recorded_at": activated_at,
        }
        baseline_path = target / "cutover-baseline.json"
        if baseline_path.exists():
            if read_json(baseline_path, base=target) != baseline:
                raise ProtocolError("cutover_facts_changed", "target baseline facts changed")
        else:
            replace_json(baseline_path, baseline, base=target)
        record = _transition(root, journal, record, "target-initialized")

        current = {
            "schema": 1,
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "event_schema": EVENT_SCHEMA,
            "state": PROTOCOL_VERSION,
            "activated_at": activated_at,
        }
        replace_json(root / ".dev-mesh" / "coord" / "current.json", current, base=root / ".dev-mesh")
        record = _transition(root, journal, record, "current-switched")
        record = _transition(root, journal, record, "ready-to-discard")
        if discard_staging.exists():
            if discard_staging.is_symlink() or not discard_staging.is_dir():
                raise ProtocolError("cutover_facts_changed", "retired source staging is unsafe")
            if tree_digest(discard_staging) != expected_source_digest:
                raise ProtocolError("cutover_facts_changed", "retired source staging changed")
            shutil.rmtree(discard_staging)
        if prior_archives_staging.exists():
            if prior_archives_staging.is_symlink() or not prior_archives_staging.is_dir():
                raise ProtocolError("cutover_facts_changed", "prior archive staging is unsafe")
            if tree_digest(prior_archives_staging) != record["prior_archives"].get("tree_sha256"):
                raise ProtocolError("cutover_facts_changed", "prior archive staging changed")
            shutil.rmtree(prior_archives_staging)
        record = _transition(root, journal, record, "completed")
        return {
            "status": "completed",
            "cutover_id": cutover_id,
            "source_disposition": "discarded",
            "analysis_retention": str(retention),
            "target_state": str(target),
            "baseline": baseline,
            "plan_digest": expected_plan_digest,
        }


def verify(root: Path, *, cutover_id: str, expected_plan_digest: str) -> dict[str, object]:
    cutover_id = _cutover_id(cutover_id)
    root = _exact_root(root)
    _assert_owned_paths(root, cutover_id=cutover_id)
    (
        source,
        target,
        retention,
        discard_staging,
        prior_archives,
        prior_archives_staging,
        _journal,
    ) = _paths(root, cutover_id)
    _path, record = _load_plan(root, cutover_id, expected_plan_digest)
    current = _raw_current(root)
    activated_at = record.get("target_created_at")
    expected_protocol = {
        "schema": 1,
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "event_schema": EVENT_SCHEMA,
        "created_at": activated_at,
        "cutover_id": cutover_id,
    }
    expected_baseline = {
        "schema": 1,
        "kind": "dev-mesh.coordination.cutover-baseline",
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "cutover_id": cutover_id,
        "source_version": SOURCE_VERSION,
        "source_state_sha256": record.get("source_state_sha256"),
        "source_disposition": "discarded-after-analysis-retention",
        "analysis_retention": record.get("analysis_retention"),
        "prior_archives_disposition": "discarded",
        "git_facts": record.get("git_facts"),
        "recorded_at": activated_at,
    }
    protocol_path = target / "protocol.json"
    baseline_path = target / "cutover-baseline.json"
    retention_summary = record.get("analysis_retention")
    verified = (
        record.get("state") == "completed"
        and isinstance(activated_at, str)
        and not source.exists()
        and not source.is_symlink()
        and not discard_staging.exists()
        and not discard_staging.is_symlink()
        and not prior_archives.exists()
        and not prior_archives.is_symlink()
        and not prior_archives_staging.exists()
        and not prior_archives_staging.is_symlink()
        and isinstance(retention_summary, dict)
        and retention.is_file()
        and not retention.is_symlink()
        and _digest(read_json(retention, base=root / ".dev-mesh"))
        == retention_summary.get("record_sha256")
        and target.is_dir()
        and not target.is_symlink()
        and all(
            (target / relative).is_dir() and not (target / relative).is_symlink()
            for relative in STATE_DIRECTORIES
        )
        and current.get("version") == PROTOCOL_VERSION
        and current.get("event_schema") == EVENT_SCHEMA
        and current.get("state") == PROTOCOL_VERSION
        and current.get("activated_at") == activated_at
        and protocol_path.is_file()
        and not protocol_path.is_symlink()
        and read_json(protocol_path, base=target) == expected_protocol
        and baseline_path.is_file()
        and not baseline_path.is_symlink()
        and read_json(baseline_path, base=target) == expected_baseline
        and git_facts(root) == record.get("git_facts")
    )
    if not verified:
        raise ProtocolError("cutover_facts_changed", "version cutover verification failed")
    return {"verified": True, "cutover_id": cutover_id, "plan_digest": expected_plan_digest}
