"""Run and direct Claim lifecycle with producer-side terminal gates."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import git_backend as git
from .constants import (
    CLAIM_INTENTS,
    CLAIM_PROJECTION_MODES,
    EVIDENCE_REQUIRED_PAUSE_BLOCKERS,
    MAX_CONTENTION_PARTICIPANTS,
    MAX_SEMANTIC_RESOURCES,
    PAUSE_BLOCKER_KINDS,
    RUN_OUTCOMES,
)
from .control_plane import ControlPlane, operation, resolve
from .events import build_event, emit, materialized, write_event
from .storage import (
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)


def _run_path(plane: ControlPlane, run_id: str) -> Path:
    return plane.state_root / "runs" / f"{run_id}.json"


def _claim_path(plane: ControlPlane, scope: str) -> Path:
    return plane.state_root / "claims" / f"{scope}.json"


def _reference_projection(items: list[dict[str, object]]) -> dict[str, object]:
    encoded = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "reference_count": len(items),
        "reference_sha256": hashlib.sha256(encoded).hexdigest(),
        "reference_sample": items[:32],
    }


def _read_run(plane: ControlPlane, run_id: str, owner: str) -> dict[str, object]:
    record = read_json(_run_path(plane, run_id), base=plane.state_root)
    if record.get("owner") != owner:
        raise ValueError(f"run {run_id!r} belongs to {record.get('owner')!r}")
    if record.get("status") != "active":
        raise ValueError(f"run {run_id!r} is not active")
    return record


def join_run(
    root: Path,
    *,
    run_id: str,
    owner: str,
    task: str,
    parent_owner: str | None = None,
) -> dict[str, object]:
    run_id = require_identifier(run_id, "run id")
    owner = require_slug(owner, "owner")
    task = require_text(task, "task", 500)
    if parent_owner is not None:
        parent_owner = require_slug(parent_owner, "parent owner")
    with operation(root, "agent-join") as plane:
        joined_revision = git.head(root)
        canonical_branch = git.branch(root)
        path = _run_path(plane, run_id)
        expected = {
            "run_id": run_id,
            "owner": owner,
            "task": task,
            "parent_owner": parent_owner,
        }
        if path.exists():
            record = read_json(path, base=plane.state_root)
            if any(record.get(key) != value for key, value in expected.items()):
                raise ValueError("run id already exists with different metadata")
            if record.get("status") != "active":
                raise ValueError("closed run cannot be joined again")
            return record
        record = materialized(
            {
                "schema": 1,
                **expected,
                "status": "active",
                "joined_at": now(),
                "joined_revision": joined_revision,
                "canonical_branch": canonical_branch,
            }
        )
        write_json_exclusive(path, record, base=plane.state_root)
        event_path, event = emit(
            plane,
            "agent-joined",
            payload={**expected, "joined_revision": joined_revision, "canonical_branch": canonical_branch},
        )
        record["joined_event_id"] = event["event_id"]
        replace_json(path, record, base=plane.state_root)
        return {**record, "event_path": str(event_path)}


def _paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _active_claims(plane: ControlPlane) -> list[dict[str, object]]:
    return [
        read_json(path, base=plane.state_root)
        for path in sorted((plane.state_root / "claims").glob("*.json"))
    ]


def _claim_conflicts(
    plane: ControlPlane,
    *,
    paths: list[str],
    semantic_writes: list[str],
    sensitive_to: list[str],
) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    for claim in _active_claims(plane):
        if claim.get("intent") == "read":
            continue
        other_paths = [item for item in claim.get("paths", []) if isinstance(item, str)]
        physical_count = 0
        physical_digest = hashlib.sha256()
        for left in sorted(paths):
            for right in sorted(other_paths):
                if _paths_overlap(left, right):
                    physical_count += 1
                    physical_digest.update(left.encode("utf-8"))
                    physical_digest.update(b"\0")
                    physical_digest.update(right.encode("utf-8"))
                    physical_digest.update(b"\0")
        other_writes = {item for item in claim.get("semantic_writes", []) if isinstance(item, str)}
        other_sensitive = {item for item in claim.get("sensitive_to", []) if isinstance(item, str)}
        semantic = sorted(
            (set(semantic_writes) & other_writes)
            | (set(semantic_writes) & other_sensitive)
            | (set(sensitive_to) & other_writes)
        )
        if physical_count or semantic:
            conflicts.append(
                {
                    "scope": claim.get("scope"),
                    "owner": claim.get("owner"),
                    "run_id": claim.get("run_id"),
                    "status": claim.get("status"),
                    "contention_id": claim.get("contention_id"),
                    "physical_overlap_count": physical_count,
                    "physical_overlap_sha256": physical_digest.hexdigest(),
                    "semantic_resources": semantic,
                }
            )
    return conflicts


def _claim_covers_request(
    claim: dict[str, object],
    *,
    paths: list[str],
    intent: str,
    projection_mode: str,
    semantic_writes: list[str],
    sensitive_to: list[str],
) -> bool:
    """Return whether one existing same-Run Claim already grants the request."""

    if claim.get("status") not in {
        "active",
        "paused",
        "pending-arbitration",
        "pending-baseline",
    }:
        return False
    if (
        claim.get("intent") != intent
        or claim.get("projection_mode", "git-tree") != projection_mode
    ):
        return False
    existing_paths = [item for item in claim.get("paths", []) if isinstance(item, str)]
    if not all(
        any(Path(existing) == Path(requested) or Path(existing) in Path(requested).parents
            for existing in existing_paths)
        for requested in paths
    ):
        return False
    return (
        set(semantic_writes).issubset(claim.get("semantic_writes", []))
        and set(sensitive_to).issubset(claim.get("sensitive_to", []))
    )


def create_claim(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    task: str,
    paths: list[str],
    intent: str = "local-edit",
    projection_mode: str = "git-tree",
    semantic_writes: list[str] | None = None,
    sensitive_to: list[str] | None = None,
    validation: str = "",
    first_release: str = "",
    allow_overlap: bool = False,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    task = require_text(task, "task", 500)
    if intent not in CLAIM_INTENTS:
        raise ValueError(f"unsupported Claim intent: {intent}")
    if projection_mode not in CLAIM_PROJECTION_MODES:
        raise ValueError(f"unsupported Claim projection mode: {projection_mode}")
    if intent == "read" and projection_mode != "git-tree":
        raise ValueError("read Claims use the default git-tree projection")
    validation = require_text(validation, "validation plan", 2000) if validation.strip() else ""
    first_release = require_text(first_release, "first release", 2000) if first_release.strip() else ""
    semantic_writes = sorted({require_identifier(item, "semantic write") for item in semantic_writes or []})
    sensitive_to = sorted({require_identifier(item, "sensitive resource") for item in sensitive_to or []})
    if len(semantic_writes) > MAX_SEMANTIC_RESOURCES or len(sensitive_to) > MAX_SEMANTIC_RESOURCES:
        raise ValueError(f"a Claim may declare at most {MAX_SEMANTIC_RESOURCES} resources per semantic set")
    normalized_paths = git.normalize_paths(root, paths)
    created: dict[str, object]
    with operation(root, "claim-create") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, scope)
        if path.exists():
            raise ValueError(f"claim already exists: {scope}")
        conflicts = [] if intent == "read" else _claim_conflicts(
            plane,
            paths=normalized_paths,
            semantic_writes=semantic_writes,
            sensitive_to=sensitive_to,
        )
        same_run_conflicts = [
            item
            for item in conflicts
            if item.get("owner") == owner and item.get("run_id") == run_id
        ]
        if same_run_conflicts:
            covering = [
                read_json(
                    _claim_path(plane, str(item["scope"])),
                    base=plane.state_root,
                )
                for item in same_run_conflicts
                if isinstance(item.get("scope"), str)
            ]
            covering = [
                item
                for item in covering
                if _claim_covers_request(
                    item,
                    paths=normalized_paths,
                    intent=intent,
                    projection_mode=projection_mode,
                    semantic_writes=semantic_writes,
                    sensitive_to=sensitive_to,
                )
            ]
            if len(covering) == 1:
                return {
                    **covering[0],
                    "claim_reused": True,
                    "requested_scope": scope,
                }
            scopes = sorted(
                str(item["scope"])
                for item in same_run_conflicts
                if isinstance(item.get("scope"), str)
            )
            raise ValueError(
                "same Run already owns overlapping Claim(s): "
                + ", ".join(scopes)
                + "; reuse a covering Claim, extend it with claim-update, or release and re-claim"
            )
        if len(conflicts) + 1 > MAX_CONTENTION_PARTICIPANTS:
            raise ValueError(
                f"overlap exceeds {MAX_CONTENTION_PARTICIPANTS} participants; decompose the scope"
            )
        in_flight = [
            item
            for item in conflicts
            if item.get("status")
            in {"pending-arbitration", "pending-baseline", "completing", "transaction"}
        ]
        if in_flight:
            raise ValueError(f"overlap already has an in-flight arbitration or transaction: {in_flight}")
        # ``allow_overlap`` remains accepted for callers from the unpublished
        # draft, but overlap is now always materialized as a non-authoritative
        # pending Claim.  Refusing before materialization left weaker callers
        # without a contention id or an actionable next step.
        _ = allow_overlap
        status = "pending-arbitration" if conflicts else "active"
        base_revision = git.head(root)
        canonical_branch = git.branch(root)
        workspace_base = None
        if projection_mode == "workspace-bytes":
            from .workspace_projection import workspace_bytes_projection

            workspace_base = workspace_bytes_projection(root, normalized_paths)
        baseline = None
        if status == "active" and intent != "read":
            from .work_results import required_baseline

            baseline = required_baseline(
                root,
                plane,
                normalized_paths,
                projection_mode=projection_mode,
                workspace_projection=workspace_base,
            )
            if baseline is not None:
                status = "pending-baseline"
        event_name = (
            "claim-requested"
            if conflicts
            else "claim-baseline-required"
            if baseline is not None
            else "claim-created"
        )
        event = build_event(
            event_name,
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "paths": normalized_paths,
                "intent": intent,
                "projection_mode": projection_mode,
                "semantic_resources": sorted(set(semantic_writes) | set(sensitive_to)),
                "status": status,
                "conflicts": conflicts,
                "base_revision": base_revision,
                "canonical_branch": canonical_branch,
                "baseline": baseline,
            },
        )
        record = materialized(
            {
                "schema": 1,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "task": task,
                "paths": normalized_paths,
                "intent": intent,
                "projection_mode": projection_mode,
                "semantic_writes": semantic_writes,
                "sensitive_to": sensitive_to,
                "validation": validation,
                "first_release": first_release,
                "status": status,
                "conflicts": conflicts,
                "created_at": now(),
                "heartbeat_at": now(),
                "base_revision": base_revision,
                "canonical_branch": canonical_branch,
                "baseline": baseline,
                "workspace_base": workspace_base if status != "pending-arbitration" else None,
            }
        )
        write_json_exclusive(path, record, base=plane.state_root)
        _, event = write_event(plane, event)
        record["created_event_id"] = event["event_id"]
        replace_json(path, record, base=plane.state_root)
        created = record
        if conflicts:
            from .contention import open_for_claim_in_operation

            contention = open_for_claim_in_operation(plane, scope=scope)
            created = {**record, "contention_id": contention["contention_id"]}
    return created


def activate_pending_claim(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    evidence: str = "",
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    with operation(root, "claim-activate") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, scope)
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") != "pending-arbitration":
            raise ValueError("only a pending-arbitration Claim can activate")
        contention_id = record.get("contention_id")
        if not isinstance(contention_id, str):
            raise ValueError("pending Claim has no correlated contention")
        archived = plane.state_root / "contentions" / "archive" / f"{contention_id}.json"
        decision = read_json(archived, base=plane.state_root)
        if decision.get("status") != "completed":
            raise ValueError("correlated contention is not completed")
        if evidence.strip():
            evidence = require_text(evidence, "activation evidence", 1000)
        elif decision.get("decision") == "wait":
            evidence = "overlap and inherited baseline rechecked under the operation lock"
        else:
            raise ValueError("activation evidence is required for non-wait decisions")
        conflicts = _claim_conflicts(
            plane,
            paths=[item for item in record.get("paths", []) if isinstance(item, str)],
            semantic_writes=[item for item in record.get("semantic_writes", []) if isinstance(item, str)],
            sensitive_to=[item for item in record.get("sensitive_to", []) if isinstance(item, str)],
        )
        conflicts = [item for item in conflicts if item.get("scope") != scope]
        if conflicts:
            raise ValueError(f"Claim still overlaps current authority or intent: {conflicts}")
        from .work_results import required_baseline

        projection_mode = str(record.get("projection_mode", "git-tree"))
        workspace_base = None
        if projection_mode == "workspace-bytes":
            from .workspace_projection import workspace_bytes_projection

            workspace_base = workspace_bytes_projection(
                root,
                [item for item in record.get("paths", []) if isinstance(item, str)],
            )
        baseline = (
            required_baseline(
                root,
                plane,
                [item for item in record.get("paths", []) if isinstance(item, str)],
                projection_mode=projection_mode,
                workspace_projection=workspace_base,
            )
            if record.get("intent") != "read"
            else None
        )
        next_status = "pending-baseline" if baseline is not None else "active"
        record.update(
            {
                "status": next_status,
                "activation_evidence": evidence,
                "activated_at": now(),
                "heartbeat_at": now(),
                "base_revision": git.head(root),
                "canonical_branch": git.branch(root),
                "conflicts": [],
                "baseline": baseline,
                "workspace_base": workspace_base,
            }
        )
        replace_json(path, record, base=plane.state_root)
        _, event = emit(
            plane,
            "claim-baseline-required" if baseline is not None else "claim-activated",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "contention_id": contention_id,
                "decision": decision.get("decision"),
                "evidence": evidence,
                "paths": record.get("paths", []),
                "base_revision": record["base_revision"],
                "canonical_branch": record["canonical_branch"],
                "baseline": baseline,
                "projection_mode": projection_mode,
                "status": next_status,
            },
        )
        record["activated_event_id"] = event["event_id"]
        replace_json(path, record, base=plane.state_root)
        return record


def update_claim(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    task: str | None = None,
    paths: list[str] | None = None,
    semantic_writes: list[str] | None = None,
    sensitive_to: list[str] | None = None,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    with operation(root, "claim-update") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, scope)
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") == "completing":
            raise ValueError("a completing Claim only accepts an exact completion retry")
        authority_changed = paths is not None or semantic_writes is not None or sensitive_to is not None
        if authority_changed and record.get("status") != "active":
            raise ValueError("only an active Claim may change its authority declaration")
        if paths is not None and record.get("projection_mode") == "workspace-bytes":
            raise ValueError("workspace-bytes Claim paths are immutable; release and re-claim")
        before = {
            key: record.get(key)
            for key in ("task", "paths", "semantic_writes", "sensitive_to", "intent", "status")
        }
        if task is not None:
            record["task"] = require_text(task, "task", 500)
        if paths is not None:
            record["paths"] = git.normalize_paths(root, paths)
        if semantic_writes is not None:
            record["semantic_writes"] = sorted(
                {require_identifier(item, "semantic write") for item in semantic_writes}
            )
        if sensitive_to is not None:
            record["sensitive_to"] = sorted(
                {require_identifier(item, "sensitive resource") for item in sensitive_to}
            )
        if len(record.get("semantic_writes", [])) > MAX_SEMANTIC_RESOURCES or len(record.get("sensitive_to", [])) > MAX_SEMANTIC_RESOURCES:
            raise ValueError(f"a Claim may declare at most {MAX_SEMANTIC_RESOURCES} resources per semantic set")
        after = {
            key: record.get(key)
            for key in ("task", "paths", "semantic_writes", "sensitive_to", "intent", "status")
        }
        record["heartbeat_at"] = now()
        if before == after:
            replace_json(path, record, base=plane.state_root)
            return {**record, "event_emitted": False}
        if authority_changed:
            conflicts = [] if record.get("intent") == "read" else _claim_conflicts(
                plane,
                paths=list(record.get("paths", [])),
                semantic_writes=list(record.get("semantic_writes", [])),
                sensitive_to=list(record.get("sensitive_to", [])),
            )
            conflicts = [item for item in conflicts if item.get("scope") != scope]
            if conflicts:
                raise ValueError(f"updated claim overlaps active authority: {conflicts}")
        record["updated_at"] = now()
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "claim-updated",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "paths": record["paths"],
                "projection_mode": record.get("projection_mode", "git-tree"),
                "semantic_resources": sorted(
                    set(record.get("semantic_writes", [])) | set(record.get("sensitive_to", []))
                ),
            },
        )
        return {**record, "event_emitted": True}


def heartbeat_claim(root: Path, *, scope: str, owner: str, run_id: str) -> dict[str, object]:
    return update_claim(root, scope=scope, owner=owner, run_id=run_id)


def pause_claim(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    blocker_kind: str,
    checkpoint: str,
    resume_condition: str,
    operation_name: str | None = None,
    resources: list[str] | None = None,
    error_kind: str | None = None,
    retain_paths_reason: str | None = None,
) -> dict[str, object]:
    if blocker_kind not in PAUSE_BLOCKER_KINDS:
        raise ValueError(f"unsupported pause blocker kind: {blocker_kind}")
    checkpoint = require_text(checkpoint, "checkpoint", 2000)
    resume_condition = require_text(resume_condition, "resume condition", 1000)
    operation_name = (
        require_text(operation_name, "operation", 500) if operation_name is not None else None
    )
    error_kind = require_identifier(error_kind, "error kind") if error_kind is not None else None
    resources = sorted({require_identifier(item, "pause resource") for item in resources or []})
    if len(resources) > MAX_SEMANTIC_RESOURCES:
        raise ValueError(f"a pause may reference at most {MAX_SEMANTIC_RESOURCES} resources")
    retain_paths_reason = (
        require_text(retain_paths_reason, "retain paths reason", 1000)
        if retain_paths_reason is not None
        else None
    )
    if blocker_kind in EVIDENCE_REQUIRED_PAUSE_BLOCKERS:
        if operation_name is None or not resources or error_kind is None:
            raise ValueError(
                f"{blocker_kind} pause requires operation, resource, and stable error kind"
            )
    with operation(root, "claim-pause") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, require_slug(scope, "scope"))
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") != "active":
            raise ValueError("only an active claim can pause")
        pause = {
            "blocker_kind": blocker_kind,
            "operation": operation_name,
            "resources": resources,
            "error_kind": error_kind,
            "resume_condition": resume_condition,
            "retain_paths_reason": retain_paths_reason,
        }
        event = build_event(
            "claim-paused",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "blocker_kind": blocker_kind,
                "operation": operation_name,
                "resources": resources,
                "error_kind": error_kind,
                "resume_condition": resume_condition,
                "retained_paths": True,
                "retain_paths_reason": retain_paths_reason,
            },
        )
        record.update(
            {
                "status": "paused",
                "pause": pause,
                "checkpoint": checkpoint,
                "paused_at": now(),
                "heartbeat_at": now(),
            }
        )
        replace_json(path, record, base=plane.state_root)
        write_event(plane, event)
        return record


def resume_claim(root: Path, *, scope: str, owner: str, run_id: str, evidence: str) -> dict[str, object]:
    with operation(root, "claim-resume") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, require_slug(scope, "scope"))
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") != "paused":
            raise ValueError("only a paused claim can resume")
        pause = record.get("pause")
        if not isinstance(pause, dict) or pause.get("blocker_kind") not in PAUSE_BLOCKER_KINDS:
            raise ValueError("paused claim has malformed blocker metadata")
        evidence = require_text(evidence, "resume evidence", 1000)
        if pause.get("blocker_kind") in EVIDENCE_REQUIRED_PAUSE_BLOCKERS and not evidence.strip():
            raise ValueError("authorization/environment resume requires current resource-state evidence")
        resumed_at = now()
        last_pause = {
            **pause,
            "checkpoint": record.pop("checkpoint", None),
            "resume_evidence": evidence,
            "resumed_at": resumed_at,
        }
        record.update(
            {
                "status": "active",
                "last_pause": last_pause,
                "resumed_at": resumed_at,
                "heartbeat_at": now(),
            }
        )
        record.pop("pause", None)
        replace_json(path, record, base=plane.state_root)
        emit(
            plane,
            "claim-resumed",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "blocker_kind": pause.get("blocker_kind"),
                "operation": pause.get("operation"),
                "resources": pause.get("resources", []),
                "error_kind": pause.get("error_kind"),
                "evidence": evidence,
            },
        )
        return record


def append_audit_correction(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    supersedes_event_id: str,
    observation: str,
    resources: list[str] | None = None,
) -> dict[str, object]:
    """Append owner-scoped evidence without rewriting the original immutable event."""

    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    supersedes_event_id = require_identifier(supersedes_event_id, "superseded event id")
    observation = require_text(observation, "correction observation", 2000)
    resources = sorted({require_identifier(item, "correction resource") for item in resources or []})
    if len(resources) > MAX_SEMANTIC_RESOURCES:
        raise ValueError(f"a correction may reference at most {MAX_SEMANTIC_RESOURCES} resources")
    with operation(root, "claim-audit-correction") as plane:
        _read_run(plane, run_id, owner)
        claim = read_json(_claim_path(plane, scope), base=plane.state_root)
        if claim.get("owner") != owner or claim.get("run_id") != run_id:
            raise ValueError("audit correction requires the exact current Claim owner and Run")
        original: dict[str, object] | None = None
        for path in sorted((plane.state_root / "events").glob("*.json")):
            candidate = read_json(path, base=plane.state_root)
            if candidate.get("event_id") == supersedes_event_id:
                original = candidate
                break
        if original is None:
            raise ValueError("superseded event does not exist in the active protocol state")
        if original.get("scope") != scope or original.get("owner") != owner:
            raise ValueError("audit correction cannot supersede another scope or owner")
        event_path, event = emit(
            plane,
            "audit-correction",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "supersedes_event_id": supersedes_event_id,
                "observation": observation,
                "resources": resources,
                "status": "corrected",
                "reason_code": "owner-observed-correction",
            },
        )
        return {**event, "event_path": str(event_path)}


def _finish_claim_release(
    root: Path,
    plane: ControlPlane,
    *,
    path: Path,
    record: dict[str, object],
    summary: str,
) -> dict[str, object]:
    """Archive one already-validated Claim release under the current operation lock."""

    scope = str(record["scope"])
    released_at = now()
    release_revision = git.head(root)
    emit(
        plane,
        "claim-released",
        payload={
            "scope": scope,
            "owner": record.get("owner"),
            "run_id": record.get("run_id"),
            "paths": record.get("paths", []),
            "summary": summary,
            "status": "released",
            "base_revision": record.get("base_revision"),
            "release_revision": release_revision,
            "canonical_branch": record.get("canonical_branch"),
            "projection_mode": record.get("projection_mode", "git-tree"),
        },
    )
    record.update(
        {
            "status": "released",
            "released_at": released_at,
            "summary": summary,
            "release_revision": release_revision,
        }
    )
    destination = plane.state_root / "archive" / "claims" / f"{time.time_ns()}-{scope}.json"
    replace_json(path, record, base=plane.state_root)
    os.replace(path, destination)
    return {**record, "archive": str(destination)}


def release_claim(root: Path, *, scope: str, owner: str, run_id: str, summary: str) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    summary = require_text(summary, "release summary", 1000)
    with operation(root, "claim-release") as plane:
        _read_run(plane, run_id, owner)
        path = _claim_path(plane, scope)
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") == "transaction":
            raise ValueError("transaction-promoted claim must finish through its transaction")
        if git.branch(root) != record.get("canonical_branch"):
            raise ValueError("canonical branch changed while the Claim was active")
        paths = [item for item in record.get("paths", []) if isinstance(item, str)]
        if record.get("intent") != "read" and record.get("status") in {"active", "paused"}:
            if record.get("projection_mode", "git-tree") == "workspace-bytes":
                from .workspace_projection import (
                    workspace_bytes_changed_paths,
                    workspace_bytes_projection,
                )

                workspace_base = record.get("workspace_base")
                if not isinstance(workspace_base, dict):
                    raise ValueError("workspace-bytes Claim lacks its accepted starting baseline")
                dirty = workspace_bytes_changed_paths(
                    workspace_base, workspace_bytes_projection(root, paths)
                )
            else:
                dirty = git.dirty_paths(root, paths)
            if dirty:
                raise ValueError(
                    "claimed paths are dirty; complete the Claim into a Work Result first: "
                    + ", ".join(dirty)
                )
        return _finish_claim_release(
            root,
            plane,
            path=path,
            record=record,
            summary=summary,
        )


def _run_blockers(plane: ControlPlane, run_id: str) -> list[dict[str, object]]:
    def blocker(kind: str, identifier: object, status: object, record: dict[str, object]) -> dict[str, object]:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "kind": kind,
            "id": identifier,
            "status": status,
            "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    blockers: list[dict[str, object]] = []
    for claim in _active_claims(plane):
        if claim.get("run_id") == run_id and claim.get("status") in {
            "active",
            "paused",
            "pending-arbitration",
            "pending-baseline",
            "completing",
        }:
            blockers.append(blocker("claim", claim.get("scope"), claim.get("status"), claim))
    for path in sorted((plane.state_root / "transactions" / "active").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        if record.get("run_id") == run_id:
            blockers.append(blocker("transaction", record.get("transaction_id"), record.get("status"), record))
    for path in sorted((plane.state_root / "direct-commits" / "active").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        if record.get("run_id") == run_id:
            blockers.append(blocker("direct-commit", record.get("direct_commit_id"), record.get("status"), record))
    for path in sorted((plane.state_root / "handoffs").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        if record.get("source_run_id") == run_id and record.get("status") == "offered":
            blockers.append(blocker("handoff", record.get("handoff_id"), "offered", record))
    for path in sorted((plane.state_root / "contentions" / "active").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        run_ids = {item for item in record.get("participant_run_ids", []) if isinstance(item, str)}
        if run_id in run_ids:
            blockers.append(blocker("contention", record.get("contention_id"), record.get("status"), record))
    for path in sorted((plane.state_root / "work" / "active").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        if record.get("run_id") == run_id:
            blockers.append(blocker("work", record.get("work_state_id"), record.get("status"), record))
    return blockers


def _reviewed_close_facts(
    plane: ControlPlane,
    *,
    run_id: str,
    record: dict[str, object],
) -> dict[str, object]:
    """Bind an operator review to one exact Run snapshot and authority set."""

    blockers = _run_blockers(plane, run_id)
    record_bytes = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    blocker_projection = _reference_projection(blockers)
    token_input = {
        "run_id": run_id,
        "run_sha256": hashlib.sha256(record_bytes).hexdigest(),
        "blockers_sha256": blocker_projection["reference_sha256"],
    }
    token_bytes = json.dumps(
        token_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "run_id": run_id,
        "owner": record.get("owner"),
        "task": record.get("task"),
        "status": record.get("status"),
        "joined_at": record.get("joined_at"),
        "last_activity_at": record.get("heartbeat_at", record.get("joined_at")),
        "run_sha256": token_input["run_sha256"],
        "review_token": hashlib.sha256(token_bytes).hexdigest(),
        "blockers": blocker_projection,
        "allowed_outcomes": (
            ["failed", "abandoned"] if blockers else sorted(RUN_OUTCOMES)
        ),
        "authority_preserved": bool(blockers),
    }


def preview_reviewed_run_close(root: Path, *, run_id: str) -> dict[str, object]:
    """Return bounded current facts for a human-reviewed Run closure."""

    run_id = require_identifier(run_id, "run id")
    with operation(root, "operator-run-close-preview") as plane:
        record = read_json(_run_path(plane, run_id), base=plane.state_root)
        if record.get("status") != "active":
            raise ValueError("only an active Run can be reviewed for closure")
        return _reviewed_close_facts(plane, run_id=run_id, record=record)


def close_run_after_review(
    root: Path,
    *,
    run_id: str,
    review_token: str,
    reviewer: str,
    outcome: str,
    reason_code: str,
    evidence: str,
) -> dict[str, object]:
    """Close one exact reviewed Run without silently discarding its authority."""

    run_id = require_identifier(run_id, "run id")
    review_token = require_identifier(review_token, "review token")
    reviewer = require_slug(reviewer, "reviewer")
    if outcome not in RUN_OUTCOMES:
        raise ValueError(f"unsupported run outcome: {outcome}")
    reason_code = require_identifier(reason_code, "reason code")
    evidence = require_text(evidence, "review evidence", 2000)
    with operation(root, "operator-run-close") as plane:
        path = _run_path(plane, run_id)
        record = read_json(path, base=plane.state_root)
        if record.get("status") == "closed":
            prior = record.get("operator_review")
            if (
                isinstance(prior, dict)
                and prior.get("review_token") == review_token
                and prior.get("reviewer") == reviewer
                and prior.get("outcome") == outcome
                and prior.get("reason_code") == reason_code
                and prior.get("evidence") == evidence
            ):
                return record
            raise ValueError("run is already closed with different terminal metadata")
        if record.get("status") != "active":
            raise ValueError("only an active Run can be closed after review")
        facts = _reviewed_close_facts(plane, run_id=run_id, record=record)
        if facts["review_token"] != review_token:
            raise ValueError("run or authority changed after review; preview again")
        if outcome not in facts["allowed_outcomes"]:
            raise ValueError("a Run with active authority cannot be closed as completed")
        attention = facts["blockers"]
        owner = str(facts["owner"])
        operator_review = {
            "review_token": review_token,
            "reviewer": reviewer,
            "outcome": outcome,
            "reason_code": reason_code,
            "evidence": evidence,
            "authority_preserved": facts["authority_preserved"],
        }
        payload = {
            "run_id": run_id,
            "owner": owner,
            "outcome": outcome,
            "summary": evidence,
            "status": outcome,
            "reason_code": reason_code,
            "closure_kind": "operator-reviewed",
            "operator_review": operator_review,
            **attention,
            "left_revision": git.head(root),
            "canonical_branch": git.branch(root),
        }
        _, event = emit(plane, "agent-left", payload=payload)
        record.update(
            {
                "status": "closed",
                "outcome": outcome,
                "summary": evidence,
                "reason_code": reason_code,
                "attention": attention,
                "operator_review": operator_review,
                "left_at": now(),
                "left_event_id": event["event_id"],
            }
        )
        replace_json(path, record, base=plane.state_root)
        return record


def leave_run(
    root: Path,
    *,
    run_id: str,
    owner: str,
    outcome: str,
    summary: str,
    force_terminal: bool = False,
    reason_code: str | None = None,
) -> dict[str, object]:
    run_id = require_identifier(run_id, "run id")
    owner = require_slug(owner, "owner")
    if outcome not in RUN_OUTCOMES:
        raise ValueError(f"unsupported run outcome: {outcome}")
    summary = require_text(summary, "run summary", 1000)
    with operation(root, "agent-leave") as plane:
        path = _run_path(plane, run_id)
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner:
            raise ValueError("run owner does not match")
        if record.get("status") == "closed":
            if record.get("outcome") == outcome and record.get("summary") == summary:
                return record
            raise ValueError("run is already closed with different terminal metadata")
        blockers = _run_blockers(plane, run_id)
        if blockers:
            if not force_terminal:
                raise ValueError(f"run still owns or participates in active coordination: {blockers}")
            if outcome == "completed":
                raise ValueError("completed run cannot force-terminal active coordination")
            if reason_code is None:
                raise ValueError("forced failed/abandoned leave requires a reason code")
            reason_code = require_identifier(reason_code, "reason code")
        attention = _reference_projection(blockers)
        payload = {
            "run_id": run_id,
            "owner": owner,
            "outcome": outcome,
            "summary": summary,
            "status": outcome,
            "reason_code": reason_code,
            **attention,
            "left_revision": git.head(root),
            "canonical_branch": git.branch(root),
        }
        _, event = emit(plane, "agent-left", payload=payload)
        record.update(
            {
                "status": "closed",
                "outcome": outcome,
                "summary": summary,
                "reason_code": reason_code,
                "attention": attention,
                "left_at": now(),
                "left_event_id": event["event_id"],
            }
        )
        replace_json(path, record, base=plane.state_root)
        return record


def recover_run_authority(
    root: Path,
    *,
    closed_run_id: str,
    owner: str,
    recovery_run_id: str,
    evidence: str,
) -> dict[str, object]:
    """Rebind stranded authority to a new active Run of the same owner.

    This is continuity for one owner identity, not cross-owner takeover. The original Run must have
    ended failed/abandoned with recorded attention, and every rebound object is revalidated under
    the operation lock.
    """

    closed_run_id = require_identifier(closed_run_id, "closed run id")
    recovery_run_id = require_identifier(recovery_run_id, "recovery run id")
    owner = require_slug(owner, "owner")
    evidence = require_text(evidence, "authority recovery evidence", 2000)
    if closed_run_id == recovery_run_id:
        raise ValueError("authority recovery requires a new Run id")

    def lineage_values(record: dict[str, object], *, field: str) -> list[str]:
        raw = record.get(field, [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("authority recovery lineage is malformed")
        lineage = list(dict.fromkeys(raw))
        if len(lineage) > 64:
            raise ValueError("authority recovery lineage exceeds the 64-Run protocol bound")
        return lineage

    def next_lineage(record: dict[str, object], *, field: str) -> list[str]:
        lineage = list(dict.fromkeys([*lineage_values(record, field=field), closed_run_id]))
        if len(lineage) > 64:
            raise ValueError("authority recovery lineage exceeds the 64-Run protocol bound")
        return lineage

    def bind_lineage(
        record: dict[str, object], *, field: str, previous_field: str
    ) -> None:
        lineage = next_lineage(record, field=field)
        record[field] = lineage
        record[previous_field] = closed_run_id

    def proves_lineage(
        record: dict[str, object], *, field: str, previous_field: str
    ) -> bool:
        lineage = lineage_values(record, field=field)
        return record.get(previous_field) == closed_run_id or closed_run_id in lineage

    def preflight_lineages(plane: ControlPlane) -> None:
        """Reject every predictable lineage error before the first authority snapshot changes."""

        for path in sorted(
            [
                *(plane.state_root / "claims").glob("*.json"),
                *(plane.state_root / "archive" / "claims").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            current = path.parent == plane.state_root / "claims"
            if current and record.get("owner") == owner and record.get("run_id") == closed_run_id:
                if record.get("status") == "completing":
                    from .work_results import validate_completion_intent

                    validate_completion_intent(plane, record)
                else:
                    if record.get("baseline_activation") is not None:
                        from .work_results import validate_baseline_activation_intent

                        validate_baseline_activation_intent(plane, record)
                    next_lineage(record, field="recovery_run_lineage")
            elif record.get("owner") == owner and record.get("run_id") == recovery_run_id:
                proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )

        for path in sorted(
            [
                *(plane.state_root / "transactions" / "active").glob("*.json"),
                *(plane.state_root / "transactions" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "transactions" / "active"
            if active and record.get("owner") == owner and record.get("run_id") == closed_run_id:
                next_lineage(record, field="recovery_run_lineage")
            elif record.get("owner") == owner and record.get("run_id") == recovery_run_id:
                proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )

        for path in sorted((plane.state_root / "direct-commits" / "active").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            if record.get("direct_commit_id") != path.stem:
                raise ValueError("active direct commit identity is malformed")
            if record.get("status") not in {
                "staging",
                "committing",
                "needs-attention",
                "completed",
            }:
                raise ValueError("active direct commit status is malformed")
            if not isinstance(record.get("owner"), str) or not isinstance(
                record.get("run_id"), str
            ):
                raise ValueError("active direct commit actor identity is malformed")

        for path in sorted((plane.state_root / "handoffs").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            if (
                record.get("source_owner") == owner
                and record.get("source_run_id") == closed_run_id
                and record.get("status") == "offered"
            ):
                next_lineage(record, field="source_recovery_run_lineage")
            elif record.get("source_owner") == owner and record.get("source_run_id") == recovery_run_id:
                proves_lineage(
                    record,
                    field="source_recovery_run_lineage",
                    previous_field="previous_source_run_id",
                )

        for path in sorted(
            [
                *(plane.state_root / "contentions" / "active").glob("*.json"),
                *(plane.state_root / "contentions" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "contentions" / "active"
            terminally_fenced = active and record.get("status") == "finalizing"
            participants = record.get("participants")
            if isinstance(participants, list):
                for participant in participants:
                    if not isinstance(participant, dict) or participant.get("owner") != owner:
                        continue
                    if participant.get("run_id") == closed_run_id:
                        if active and not terminally_fenced:
                            next_lineage(participant, field="recovery_run_lineage")
                        else:
                            lineage_values(participant, field="recovery_run_lineage")
                    elif participant.get("run_id") == recovery_run_id:
                        proves_lineage(
                            participant,
                            field="recovery_run_lineage",
                            previous_field="previous_run_id",
                        )
            coordinator = record.get("coordinator")
            if isinstance(coordinator, dict) and coordinator.get("owner") == owner:
                if coordinator.get("run_id") == closed_run_id:
                    if active and not terminally_fenced:
                        next_lineage(coordinator, field="recovery_run_lineage")
                    else:
                        lineage_values(coordinator, field="recovery_run_lineage")
                elif coordinator.get("run_id") == recovery_run_id:
                    proves_lineage(
                        coordinator,
                        field="recovery_run_lineage",
                        previous_field="previous_run_id",
                    )

        for path in sorted(
            [
                *(plane.state_root / "work" / "active").glob("*.json"),
                *(plane.state_root / "work" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "work" / "active"
            terminally_fenced = active and record.get("status") == "finalizing"
            if active and record.get("owner") == owner and record.get("run_id") == closed_run_id:
                if terminally_fenced:
                    lineage_values(record, field="recovery_run_lineage")
                else:
                    next_lineage(record, field="recovery_run_lineage")
            elif record.get("owner") == owner and record.get("run_id") == recovery_run_id:
                proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )

    with operation(root, "run-authority-recover") as plane:
        closed_path = _run_path(plane, closed_run_id)
        closed = read_json(closed_path, base=plane.state_root)
        if closed.get("owner") != owner or closed.get("status") != "closed":
            raise ValueError("authority recovery requires the exact closed owner Run")
        if closed.get("outcome") not in {"failed", "abandoned"}:
            raise ValueError("completed Runs cannot recover authority through failure continuity")
        attention = closed.get("attention")
        attention_count = (
            attention.get("reference_count") if isinstance(attention, dict) else len(attention)
            if isinstance(attention, list)
            else 0
        )
        if not isinstance(attention_count, int) or attention_count <= 0:
            raise ValueError("closed Run recorded no stranded coordination authority")
        recovery = _read_run(plane, recovery_run_id, owner)
        previous = closed.get("authority_recovered_to")
        if previous is not None:
            if previous == recovery_run_id:
                return closed
            raise ValueError("closed Run authority was already recovered to another Run")

        existing_event: dict[str, object] | None = None
        for event_path in sorted((plane.state_root / "events").glob("*.json")):
            candidate = read_json(event_path, base=plane.state_root)
            if (
                candidate.get("event") == "run-authority-recovered"
                and candidate.get("owner") == owner
                and candidate.get("source_run_id") == closed_run_id
                and candidate.get("run_id") == recovery_run_id
            ):
                existing_event = candidate

        preflight_lineages(plane)
        rebound: list[dict[str, object]] = []
        for path in sorted(
            [
                *(plane.state_root / "claims").glob("*.json"),
                *(plane.state_root / "archive" / "claims").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            current = path.parent == plane.state_root / "claims"
            if current and record.get("owner") == owner and record.get("run_id") == closed_run_id:
                if record.get("status") == "completing":
                    from .work_results import _finish_completion, validate_completion_intent

                    result, terminal_event = validate_completion_intent(plane, record)
                    _finish_completion(plane, path, record, result, terminal_event)
                    rebound.append(
                        {"kind": "claim-completion", "id": record.get("scope")}
                    )
                else:
                    baseline_finished = False
                    if record.get("baseline_activation") is not None:
                        from .work_results import (
                            _finish_baseline_activation,
                            validate_baseline_activation_intent,
                        )

                        intent, event = validate_baseline_activation_intent(plane, record)
                        record = _finish_baseline_activation(
                            plane, path, record, intent, event
                        )
                        baseline_finished = True
                    record.update({"run_id": recovery_run_id, "authority_recovered_at": now()})
                    bind_lineage(
                        record,
                        field="recovery_run_lineage",
                        previous_field="previous_run_id",
                    )
                    replace_json(path, record, base=plane.state_root)
                    rebound.append(
                        {
                            "kind": "claim-baseline" if baseline_finished else "claim",
                            "id": record.get("scope"),
                        }
                    )
            elif (
                record.get("owner") == owner
                and record.get("run_id") == recovery_run_id
                and proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
            ):
                rebound.append({"kind": "claim", "id": record.get("scope")})

        for path in sorted(
            [
                *(plane.state_root / "transactions" / "active").glob("*.json"),
                *(plane.state_root / "transactions" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "transactions" / "active"
            if active and record.get("owner") == owner and record.get("run_id") == closed_run_id:
                record.update({"run_id": recovery_run_id, "authority_recovered_at": now()})
                bind_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
                replace_json(path, record, base=plane.state_root)
                rebound.append({"kind": "transaction", "id": record.get("transaction_id")})
            elif (
                record.get("owner") == owner
                and record.get("run_id") == recovery_run_id
                and proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
            ):
                rebound.append({"kind": "transaction", "id": record.get("transaction_id")})

        # The active snapshot is the canonical Git mutation intent. Authority recovery must not
        # rewrite its exact actor identity while staging/commit facts may still need reconciliation.
        for path in sorted((plane.state_root / "direct-commits" / "active").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            if record.get("owner") == owner and record.get("run_id") == closed_run_id:
                rebound.append(
                    {"kind": "direct-commit", "id": record.get("direct_commit_id")}
                )

        for path in sorted((plane.state_root / "handoffs").glob("*.json")):
            record = read_json(path, base=plane.state_root)
            if (
                record.get("source_owner") == owner
                and record.get("source_run_id") == closed_run_id
                and record.get("status") == "offered"
            ):
                record.update(
                    {"source_run_id": recovery_run_id, "authority_recovered_at": now()}
                )
                bind_lineage(
                    record,
                    field="source_recovery_run_lineage",
                    previous_field="previous_source_run_id",
                )
                replace_json(path, record, base=plane.state_root)
                rebound.append({"kind": "handoff", "id": record.get("handoff_id")})
            elif (
                record.get("source_owner") == owner
                and record.get("source_run_id") == recovery_run_id
                and proves_lineage(
                    record,
                    field="source_recovery_run_lineage",
                    previous_field="previous_source_run_id",
                )
            ):
                rebound.append({"kind": "handoff", "id": record.get("handoff_id")})

        for path in sorted(
            [
                *(plane.state_root / "contentions" / "active").glob("*.json"),
                *(plane.state_root / "contentions" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "contentions" / "active"
            changed = False
            referenced = False
            terminally_fenced = active and record.get("status") == "finalizing"
            participants = record.get("participants")
            if isinstance(participants, list):
                for participant in participants:
                    if (
                        isinstance(participant, dict)
                        and participant.get("owner") == owner
                        and participant.get("run_id") == closed_run_id
                        and active
                        and not terminally_fenced
                    ):
                        participant["run_id"] = recovery_run_id
                        bind_lineage(
                            participant,
                            field="recovery_run_lineage",
                            previous_field="previous_run_id",
                        )
                        changed = True
                        referenced = True
                    elif (
                        isinstance(participant, dict)
                        and participant.get("owner") == owner
                        and participant.get("run_id") == closed_run_id
                        and terminally_fenced
                    ):
                        referenced = True
                    elif (
                        isinstance(participant, dict)
                        and participant.get("owner") == owner
                        and participant.get("run_id") == recovery_run_id
                        and proves_lineage(
                            participant,
                            field="recovery_run_lineage",
                            previous_field="previous_run_id",
                        )
                    ):
                        referenced = True
                if changed:
                    record["participant_run_ids"] = sorted(
                        str(item.get("run_id")) for item in participants if isinstance(item, dict)
                    )
            coordinator = record.get("coordinator")
            if (
                isinstance(coordinator, dict)
                and coordinator.get("owner") == owner
                and coordinator.get("run_id") == closed_run_id
                and active
                and not terminally_fenced
            ):
                coordinator["run_id"] = recovery_run_id
                bind_lineage(
                    coordinator,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
                record["coordinator"] = coordinator
                changed = True
                referenced = True
            elif (
                isinstance(coordinator, dict)
                and coordinator.get("owner") == owner
                and coordinator.get("run_id") == closed_run_id
                and terminally_fenced
            ):
                referenced = True
            elif (
                isinstance(coordinator, dict)
                and coordinator.get("owner") == owner
                and coordinator.get("run_id") == recovery_run_id
                and proves_lineage(
                    coordinator,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
            ):
                referenced = True
            if changed:
                record["authority_recovered_at"] = now()
                replace_json(path, record, base=plane.state_root)
            if referenced:
                rebound.append({"kind": "contention", "id": record.get("contention_id")})

        for path in sorted(
            [
                *(plane.state_root / "work" / "active").glob("*.json"),
                *(plane.state_root / "work" / "archive").glob("*.json"),
            ]
        ):
            record = read_json(path, base=plane.state_root)
            active = path.parent == plane.state_root / "work" / "active"
            terminally_fenced = active and record.get("status") == "finalizing"
            if (
                active
                and not terminally_fenced
                and record.get("owner") == owner
                and record.get("run_id") == closed_run_id
            ):
                record.update({"run_id": recovery_run_id, "authority_recovered_at": now()})
                bind_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
                replace_json(path, record, base=plane.state_root)
                rebound.append({"kind": "work", "id": record.get("work_state_id")})
            elif (
                terminally_fenced
                and record.get("owner") == owner
                and record.get("run_id") == closed_run_id
            ):
                rebound.append({"kind": "work", "id": record.get("work_state_id")})
            elif (
                record.get("owner") == owner
                and record.get("run_id") == recovery_run_id
                and proves_lineage(
                    record,
                    field="recovery_run_lineage",
                    previous_field="previous_run_id",
                )
            ):
                rebound.append({"kind": "work", "id": record.get("work_state_id")})

        if not rebound:
            raise ValueError("no recoverable authority still references the closed Run")
        if existing_event is None:
            _, event = emit(
                plane,
                "run-authority-recovered",
                payload={
                    "owner": owner,
                    "source_run_id": closed_run_id,
                    "run_id": recovery_run_id,
                    **_reference_projection(rebound),
                    "evidence": evidence,
                    "status": "recovered",
                    "reason_code": "same-owner-run-continuity",
                },
            )
        else:
            event = existing_event
        recovered_at = now()
        closed.update(
            {
                "authority_recovered_to": recovery_run_id,
                "authority_recovered_at": recovered_at,
                "authority_recovery_event_id": event["event_id"],
                "authority_recovery_evidence": evidence,
            }
        )
        replace_json(closed_path, closed, base=plane.state_root)
        return {**closed, "recovered_objects": rebound}


def status(root: Path) -> dict[str, object]:
    plane = resolve(root)
    runs = [read_json(path, base=plane.state_root) for path in sorted((plane.state_root / "runs").glob("*.json"))]
    claims = _active_claims(plane)
    return {
        "protocol": plane.version,
        "runs": runs,
        "claims": claims,
        "blockers": {
            str(run.get("run_id")): _run_blockers(plane, str(run.get("run_id")))
            for run in runs
            if run.get("status") == "active"
        },
    }
