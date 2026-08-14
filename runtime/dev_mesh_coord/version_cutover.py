"""Crash-retryable cutover from the retired coordination state version."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import git_backend as git
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


SOURCE_VERSION = "20260812.1"
SOURCE_EVENT_SCHEMA = 1


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _paths(root: Path, cutover_id: str) -> tuple[Path, Path, Path, Path]:
    coord = root / ".dev-mesh" / "coord"
    source = coord / SOURCE_VERSION
    target = coord / PROTOCOL_VERSION
    archive = coord / "archive" / cutover_id / SOURCE_VERSION
    journal = coord / "cutovers" / f"{cutover_id}.json"
    return source, target, archive, journal


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
        coord / "archive",
        coord / "archive" / cutover_id,
        coord / "cutovers",
    ):
        if parent.exists():
            ensure_regular_directory(parent, "version cutover state")


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
    source, target, archive, journal = _paths(root, cutover_id)
    with advisory_lock(root / ".dev-mesh.bootstrap.lock"):
        _assert_owned_paths(root, cutover_id=cutover_id)
        current = _raw_current(root)
        if current.get("version") != SOURCE_VERSION or current.get("event_schema") != SOURCE_EVENT_SCHEMA:
            raise ProtocolError("unsupported_protocol", "version cutover requires the exact retired source version")
        if target.exists() or archive.exists() or journal.exists():
            raise ProtocolError("already_exists", "version cutover id or target already exists")
        source_digest = tree_digest(source)
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
            "git_facts": git_facts(root),
            "planned_at": now(),
        }
        plan["plan_digest"] = _digest(plan)
        journal.parent.mkdir(parents=True, exist_ok=True)
        replace_json(journal, {**plan, "state": "planned"}, base=root / ".dev-mesh")
        return plan


def _load_plan(root: Path, cutover_id: str, expected_digest: str) -> tuple[Path, dict[str, object]]:
    _source, _target, _archive, journal = _paths(root, cutover_id)
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
    confirm_discard_old_authority: bool,
) -> dict[str, object]:
    if not confirm_agents_stopped:
        raise ProtocolError("cutover_confirmation_required", "confirm that all old writers stopped")
    cutover_id = _cutover_id(cutover_id)
    root = _exact_root(root)
    source, target, archive, journal_path = _paths(root, cutover_id)
    with advisory_lock(root / ".dev-mesh.bootstrap.lock"):
        _assert_owned_paths(root, cutover_id=cutover_id)
        journal, record = _load_plan(root, cutover_id, expected_plan_digest)
        inventory = record.get("authority_inventory")
        active_total = sum(inventory.values()) if isinstance(inventory, dict) else 0
        if active_total and not confirm_discard_old_authority:
            raise ProtocolError(
                "cutover_confirmation_required",
                "retired state contains authority; explicit discard confirmation is required",
            )
        if git_facts(root) != record.get("git_facts"):
            raise ProtocolError("cutover_facts_changed", "Git or dirty baseline changed after review")
        expected_source_digest = str(record["source_state_sha256"])
        if source.exists():
            if tree_digest(source) != expected_source_digest:
                raise ProtocolError("cutover_facts_changed", "retired state changed after review")
            archive.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, archive)
        elif not archive.exists() or tree_digest(archive) != expected_source_digest:
            raise ProtocolError("cutover_facts_changed", "retired state is neither live nor exactly archived")
        record = _transition(root, journal, record, "source-archived")

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
        record = _transition(root, journal, record, "completed")
        return {
            "status": "completed",
            "cutover_id": cutover_id,
            "source_archive": str(archive),
            "target_state": str(target),
            "baseline": baseline,
            "plan_digest": expected_plan_digest,
        }


def verify(root: Path, *, cutover_id: str, expected_plan_digest: str) -> dict[str, object]:
    cutover_id = _cutover_id(cutover_id)
    root = _exact_root(root)
    _assert_owned_paths(root, cutover_id=cutover_id)
    source, target, archive, _journal = _paths(root, cutover_id)
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
        "git_facts": record.get("git_facts"),
        "recorded_at": activated_at,
    }
    protocol_path = target / "protocol.json"
    baseline_path = target / "cutover-baseline.json"
    verified = (
        record.get("state") == "completed"
        and isinstance(activated_at, str)
        and not source.exists()
        and not source.is_symlink()
        and archive.is_dir()
        and not archive.is_symlink()
        and tree_digest(archive) == record.get("source_state_sha256")
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
