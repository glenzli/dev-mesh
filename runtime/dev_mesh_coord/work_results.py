"""Commit-independent Claim completion and dirty-baseline acknowledgement."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import git_backend as git
from .control_plane import ControlPlane, operation
from .events import build_event, materialized, write_event
from .storage import (
    now,
    read_json,
    replace_json,
    require_identifier,
    require_slug,
    require_text,
    write_json_exclusive,
)
from .workspace_projection import (
    declared_projection,
    path_projection,
    within,
    workspace_bytes_changed_paths,
    workspace_bytes_projection,
)


MAX_RELATED_RESULTS = 16


def _claim_path(plane: ControlPlane, scope: str) -> Path:
    return plane.state_root / "claims" / f"{scope}.json"


def _result_path(plane: ControlPlane, result_id: str) -> Path:
    return plane.state_root / "work-results" / f"{result_id}.json"


def _run_path(plane: ControlPlane, run_id: str) -> Path:
    return plane.state_root / "runs" / f"{run_id}.json"


def _assert_active_run(plane: ControlPlane, owner: str, run_id: str) -> None:
    record = read_json(_run_path(plane, run_id), base=plane.state_root)
    if record.get("owner") != owner or record.get("status") != "active":
        raise ValueError("operation requires the exact active Run")


def _matching_event_paths(plane: ControlPlane, event: dict[str, object]) -> list[Path]:
    event_id = require_identifier(str(event.get("event_id")), "event id")
    event_name = require_identifier(str(event.get("event")), "event name")
    return sorted((plane.state_root / "events").glob(f"*-{event_id}-{event_name}.json"))


def _ensure_event(plane: ControlPlane, event: dict[str, object]) -> None:
    matches = _matching_event_paths(plane, event)
    if len(matches) > 1:
        raise ValueError("work-result terminal event id is duplicated")
    if matches:
        if read_json(matches[0], base=plane.state_root) != event:
            raise ValueError("work-result terminal event facts changed")
        return
    write_event(plane, event)


def _related_results(plane: ControlPlane, paths: list[str]) -> list[str]:
    related: list[tuple[str, str]] = []
    for path in sorted((plane.state_root / "work-results").glob("*.json")):
        record = read_json(path, base=plane.state_root)
        result_paths = [item for item in record.get("paths", []) if isinstance(item, str)]
        if any(within(left, right) or within(right, left) for left in paths for right in result_paths):
            related.append((str(record.get("completed_at", "")), str(record.get("result_id"))))
    return [result_id for _at, result_id in sorted(related, reverse=True)[:MAX_RELATED_RESULTS]]


def _projection_mode(record: dict[str, object]) -> str:
    value = record.get("projection_mode", "git-tree")
    if value not in {"git-tree", "workspace-bytes"}:
        raise ValueError("Claim projection mode is malformed")
    return str(value)


def _baseline(
    projection: dict[str, object],
    *,
    revision: str,
    branch: str,
    dirty: list[str],
    related_result_ids: list[str],
) -> dict[str, object]:
    baseline = {
        "projection_mode": projection["projection_mode"],
        "observed_revision": revision,
        "canonical_branch": branch,
        "dirty_paths": dirty,
        "baseline_sha256": projection["declared_content_sha256"],
        "actual_path_count": projection["actual_path_count"],
        "actual_paths_sha256": projection["actual_paths_sha256"],
        "actual_path_sample": projection["actual_path_sample"],
        "related_result_ids": related_result_ids,
    }
    for field in (
        "workspace_bytes_sha256",
        "workspace_file_count",
        "workspace_missing_path_count",
        "workspace_total_bytes",
    ):
        if field in projection:
            baseline[field] = projection[field]
    encoded = json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**baseline, "evidence_sha256": hashlib.sha256(encoded).hexdigest()}


def required_baseline(
    root: Path,
    plane: ControlPlane,
    paths: list[str],
    *,
    projection_mode: str = "git-tree",
    workspace_projection: dict[str, object] | None = None,
) -> dict[str, object] | None:
    revision = git.head(root)
    branch = git.branch(root)
    if projection_mode == "git-tree":
        dirty = git.dirty_paths(root, paths)
        if not dirty:
            return None
        projection = declared_projection(
            root, plane, paths, revision, require_changes=True
        )
    elif projection_mode == "workspace-bytes":
        projection = workspace_projection or workspace_bytes_projection(root, paths)
        dirty = [
            item for item in projection.get("actual_paths", []) if isinstance(item, str)
        ]
        if not dirty:
            return None
    else:
        raise ValueError(f"unsupported Claim projection mode: {projection_mode}")
    return _baseline(
        projection,
        revision=revision,
        branch=branch,
        dirty=dirty,
        related_result_ids=_related_results(plane, paths),
    )


def _same_baseline(left: dict[str, object], right: dict[str, object]) -> bool:
    """Compare only the authority-bearing identity for the selected projection."""

    if (
        left.get("projection_mode") == "workspace-bytes"
        and right.get("projection_mode") == "workspace-bytes"
    ):
        return left.get("baseline_sha256") == right.get("baseline_sha256")
    return all(
        left.get(field) == right.get(field)
        for field in ("baseline_sha256", "observed_revision", "canonical_branch")
    )


def validate_baseline_activation_intent(
    plane: ControlPlane, claim: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate a sealed baseline grant before recovery changes any authority."""

    if claim.get("status") != "pending-baseline":
        raise ValueError("Claim is not a pending baseline activation")
    intent = claim.get("baseline_activation")
    if not isinstance(intent, dict):
        raise ValueError("pending-baseline Claim lacks a durable activation intent")
    event = intent.get("event")
    if not isinstance(event, dict):
        raise ValueError("baseline activation intent lacks exact event evidence")
    identity = {
        "scope": claim.get("scope"),
        "owner": claim.get("owner"),
        "run_id": claim.get("run_id"),
    }
    if (
        event.get("event") not in {"claim-baseline-accepted", "claim-activated"}
        or any(event.get(key) != value for key, value in identity.items())
        or event.get("event_id") != intent.get("event_id")
        or event.get("status") != "active"
        or not isinstance(intent.get("base_revision"), str)
        or not isinstance(intent.get("canonical_branch"), str)
        or not isinstance(intent.get("requested_baseline_sha256"), str)
        or intent.get("projection_mode", "git-tree") != _projection_mode(claim)
    ):
        raise ValueError("baseline activation intent is malformed")
    if event.get("event") == "claim-baseline-accepted" and (
        event.get("baseline_sha256") != intent.get("requested_baseline_sha256")
    ):
        raise ValueError("baseline acceptance event differs from its durable intent")
    if _projection_mode(claim) == "workspace-bytes" and not isinstance(
        intent.get("workspace_base"), dict
    ):
        raise ValueError("workspace-bytes activation lacks an exact workspace baseline")
    matches = _matching_event_paths(plane, event)
    if len(matches) > 1 or (
        matches and read_json(matches[0], base=plane.state_root) != event
    ):
        raise ValueError("baseline activation event facts changed")
    return intent, event


def _finish_baseline_activation(
    plane: ControlPlane,
    claim_path: Path,
    claim: dict[str, object],
    intent: dict[str, object],
    event: dict[str, object],
) -> dict[str, object]:
    """Finish event-first from a durable intent, making retries idempotent."""

    _ensure_event(plane, event)
    cleared = event.get("event") == "claim-activated"
    claim.update(
        {
            "status": "active",
            "base_revision": intent["base_revision"],
            "canonical_branch": intent["canonical_branch"],
            "baseline_accepted_event_id": event["event_id"],
            "heartbeat_at": now(),
        }
    )
    if cleared:
        claim.update({"baseline": None, "baseline_cleared_at": event.get("at")})
    else:
        claim["baseline_accepted_at"] = event.get("at")
    if _projection_mode(claim) == "workspace-bytes":
        claim["workspace_base"] = intent["workspace_base"]
    claim.pop("baseline_activation", None)
    replace_json(claim_path, claim, base=plane.state_root)
    return {
        **claim,
        "baseline_changed": cleared,
        "baseline_accepted": not cleared,
    }


def accept_baseline(
    root: Path,
    *,
    scope: str,
    owner: str,
    run_id: str,
    baseline_sha256: str,
) -> dict[str, object]:
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    baseline_sha256 = require_identifier(baseline_sha256, "baseline digest")
    with operation(root, "claim-baseline-accept") as plane:
        _assert_active_run(plane, owner, run_id)
        path = _claim_path(plane, scope)
        record = read_json(path, base=plane.state_root)
        if record.get("owner") != owner or record.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if record.get("status") != "pending-baseline":
            raise ValueError("only a pending-baseline Claim can accept inherited work")
        sealed = record.get("baseline_activation")
        if sealed is not None:
            intent, event = validate_baseline_activation_intent(plane, record)
            if intent.get("requested_baseline_sha256") != baseline_sha256:
                return {**record, "baseline_changed": True, "baseline_accepted": False}
            return _finish_baseline_activation(plane, path, record, intent, event)
        expected = record.get("baseline")
        if not isinstance(expected, dict):
            raise ValueError("pending-baseline Claim has no baseline evidence")
        if expected.get("baseline_sha256") != baseline_sha256:
            return {**record, "baseline_changed": True, "baseline_accepted": False}
        paths = [item for item in record.get("paths", []) if isinstance(item, str)]
        projection_mode = _projection_mode(record)
        workspace_projection = (
            workspace_bytes_projection(root, paths)
            if projection_mode == "workspace-bytes"
            else None
        )
        current = required_baseline(
            root,
            plane,
            paths,
            projection_mode=projection_mode,
            workspace_projection=workspace_projection,
        )
        if current is None:
            event = build_event(
                "claim-activated",
                payload={
                    "scope": scope,
                    "owner": owner,
                    "run_id": run_id,
                    "status": "active",
                    "reason_code": "inherited-baseline-no-longer-dirty",
                },
            )
            intent = {
                "requested_baseline_sha256": baseline_sha256,
                "base_revision": git.head(root),
                "canonical_branch": git.branch(root),
                "event_id": event["event_id"],
                "event": event,
                "projection_mode": projection_mode,
            }
            if workspace_projection is not None:
                intent["workspace_base"] = workspace_projection
            record["baseline_activation"] = intent
            replace_json(path, record, base=plane.state_root)
            return _finish_baseline_activation(plane, path, record, intent, event)
        if not _same_baseline(expected, current):
            record.update(
                {
                    "baseline": current,
                    "baseline_refreshed_at": now(),
                    "heartbeat_at": now(),
                }
            )
            replace_json(path, record, base=plane.state_root)
            return {**record, "baseline_changed": True, "baseline_accepted": False}
        event = build_event(
            "claim-baseline-accepted",
            payload={
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "status": "active",
                "baseline_sha256": baseline_sha256,
                "related_result_ids": expected.get("related_result_ids", []),
            },
        )
        intent = {
            "requested_baseline_sha256": baseline_sha256,
            "base_revision": current["observed_revision"],
            "canonical_branch": current["canonical_branch"],
            "event_id": event["event_id"],
            "event": event,
            "projection_mode": projection_mode,
        }
        if workspace_projection is not None:
            intent["workspace_base"] = workspace_projection
        record["baseline_activation"] = intent
        replace_json(path, record, base=plane.state_root)
        return _finish_baseline_activation(plane, path, record, intent, event)


def _finish_completion(
    plane: ControlPlane,
    claim_path: Path,
    claim: dict[str, object],
    result: dict[str, object],
    terminal_event: dict[str, object],
) -> dict[str, object]:
    result_id = str(result["result_id"])
    result_path = _result_path(plane, result_id)
    if result_path.exists():
        if read_json(result_path, base=plane.state_root) != result:
            raise ValueError("work result id already exists with different facts")
    else:
        write_json_exclusive(result_path, result, base=plane.state_root)
    _ensure_event(plane, terminal_event)
    claim.update(
        {
            "status": "completed",
            "completed_at": result["completed_at"],
            "result_id": result_id,
        }
    )
    replace_json(claim_path, claim, base=plane.state_root)
    destination = (
        plane.state_root
        / "archive"
        / "claims"
        / f"{claim.get('created_event_id', result_id)}-{claim['scope']}.json"
    )
    if destination.exists():
        if read_json(destination, base=plane.state_root) != claim:
            raise ValueError("completed Claim archive facts changed")
        claim_path.unlink(missing_ok=True)
    else:
        os.replace(claim_path, destination)
    return {**result, "claim_archive": str(destination)}


def validate_completion_intent(
    plane: ControlPlane, claim: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate every deterministic completion fact before recovery mutates other authority."""

    if claim.get("status") != "completing":
        raise ValueError("Claim is not a completion intent")
    result = claim.get("completion_result")
    terminal_event = claim.get("terminal_event")
    if not isinstance(result, dict) or not isinstance(terminal_event, dict):
        raise ValueError("completing Claim lacks durable result intent")
    identity = {
        "scope": claim.get("scope"),
        "owner": claim.get("owner"),
        "run_id": claim.get("run_id"),
    }
    if any(result.get(key) != value for key, value in identity.items()):
        raise ValueError("completion result actor identity differs from its Claim")
    if (
        terminal_event.get("event") != "claim-completed"
        or terminal_event.get("result_id") != result.get("result_id")
        or any(terminal_event.get(key) != value for key, value in identity.items())
        or result.get("terminal_event") != terminal_event
    ):
        raise ValueError("Claim completion terminal intent is malformed")
    result_id = require_identifier(str(result.get("result_id")), "result id")
    existing = _result_path(plane, result_id)
    if existing.exists() and read_json(existing, base=plane.state_root) != result:
        raise ValueError("work result id already exists with different facts")
    matches = _matching_event_paths(plane, terminal_event)
    if len(matches) > 1 or (
        matches and read_json(matches[0], base=plane.state_root) != terminal_event
    ):
        raise ValueError("work-result terminal event facts changed")
    return result, terminal_event


def complete_claim(
    root: Path,
    *,
    result_id: str,
    scope: str,
    owner: str,
    run_id: str,
    summary: str,
    validation_evidence: str,
    release_if_unchanged: bool = False,
) -> dict[str, object]:
    result_id = require_identifier(result_id, "result id")
    scope = require_slug(scope, "scope")
    owner = require_slug(owner, "owner")
    run_id = require_identifier(run_id, "run id")
    summary = require_text(summary, "completion summary", 1000)
    validation_evidence = require_text(validation_evidence, "validation evidence", 2000)
    with operation(root, "claim-complete") as plane:
        result_path = _result_path(plane, result_id)
        claim_path = _claim_path(plane, scope)
        if not claim_path.exists():
            if release_if_unchanged and not result_path.exists():
                matches = [
                    (path, read_json(path, base=plane.state_root))
                    for path in sorted(
                        (plane.state_root / "archive" / "claims").glob(f"*-{scope}.json")
                    )
                ]
                matches = [
                    (path, record)
                    for path, record in matches
                    if record.get("finish_id") == result_id
                    and record.get("owner") == owner
                    and record.get("run_id") == run_id
                ]
                if len(matches) != 1:
                    raise FileNotFoundError(claim_path)
                archive_path, released = matches[0]
                if (
                    released.get("summary") != summary
                    or released.get("finish_validation_evidence") != validation_evidence
                    or released.get("status") != "released"
                ):
                    raise ValueError("unchanged Claim finish retry differs from its archived facts")
                return {
                    **released,
                    "archive": str(archive_path),
                    "completion_kind": "released-unchanged",
                    "work_result_created": False,
                }
            result = read_json(result_path, base=plane.state_root)
            if any(
                result.get(key) != value
                for key, value in (("scope", scope), ("owner", owner), ("run_id", run_id))
            ):
                raise ValueError("completed work result identity does not match")
            terminal_event = result.get("terminal_event")
            if not isinstance(terminal_event, dict):
                raise ValueError("completed work result lacks terminal event evidence")
            _ensure_event(plane, terminal_event)
            return result
        _assert_active_run(plane, owner, run_id)
        claim = read_json(claim_path, base=plane.state_root)
        if claim.get("owner") != owner or claim.get("run_id") != run_id:
            raise ValueError("claim owner or run does not match")
        if claim.get("intent") == "read":
            raise ValueError("read Claims release without creating a Work Result")
        if claim.get("status") == "completing":
            result, terminal_event = validate_completion_intent(plane, claim)
            if result.get("result_id") != result_id:
                raise ValueError("Claim completion is already bound to another result id")
            return _finish_completion(plane, claim_path, claim, result, terminal_event)
        if claim.get("status") != "active":
            raise ValueError("only an active writable Claim can complete")
        paths = [item for item in claim.get("paths", []) if isinstance(item, str)]
        completion_revision = git.head(root)
        if git.branch(root) != claim.get("canonical_branch"):
            raise ValueError("canonical branch changed while the Claim was active")
        projection_mode = _projection_mode(claim)
        if projection_mode == "git-tree":
            projection = declared_projection(
                root, plane, paths, completion_revision, require_changes=False
            )
            actual = [
                item for item in projection.get("actual_paths", []) if isinstance(item, str)
            ]
        else:
            projection = workspace_bytes_projection(root, paths)
            workspace_base = claim.get("workspace_base")
            if not isinstance(workspace_base, dict):
                raise ValueError("workspace-bytes Claim lacks its accepted starting baseline")
            actual = workspace_bytes_changed_paths(workspace_base, projection)
            projection = {**projection, "actual_paths": actual, **path_projection(actual)}
        accepted_baseline = claim.get("baseline")
        unchanged_from_accepted_baseline = (
            isinstance(accepted_baseline, dict)
            and isinstance(claim.get("baseline_accepted_at"), str)
            and projection.get("declared_content_sha256")
            == accepted_baseline.get("baseline_sha256")
        )
        if release_if_unchanged and (
            projection.get("actual_path_count") == 0
            or unchanged_from_accepted_baseline
        ):
            from .lifecycle import _finish_claim_release

            claim.update(
                {
                    "finish_id": result_id,
                    "finish_validation_evidence": validation_evidence,
                }
            )
            released = _finish_claim_release(
                root,
                plane,
                path=claim_path,
                record=claim,
                summary=summary,
            )
            return {
                **released,
                "completion_kind": "released-unchanged",
                "work_result_created": False,
            }
        completed_at = now()
        terminal_event = build_event(
            "claim-completed",
            payload={
                "result_id": result_id,
                "scope": scope,
                "owner": owner,
                "run_id": run_id,
                "status": "completed",
                "base_revision": claim.get("base_revision"),
                "completion_revision": completion_revision,
                "canonical_branch": claim.get("canonical_branch"),
                "projection_mode": projection_mode,
                "actual_path_count": projection["actual_path_count"],
                "actual_paths_sha256": projection["actual_paths_sha256"],
                "actual_path_sample": projection["actual_path_sample"],
            },
        )
        result_fields: dict[str, object] = {
            "schema": 1,
            "kind": "dev-mesh.work-result",
            "result_id": result_id,
            "status": "recorded",
            "scope": scope,
            "owner": owner,
            "run_id": run_id,
            "task": claim.get("task"),
            "paths": paths,
            "semantic_writes": claim.get("semantic_writes", []),
            "sensitive_to": claim.get("sensitive_to", []),
            "base_revision": claim.get("base_revision"),
            "completion_revision": completion_revision,
            "canonical_branch": claim.get("canonical_branch"),
            "projection_mode": projection_mode,
            "dirty_paths": (
                git.dirty_paths(root, paths)
                if projection_mode == "git-tree"
                else actual
            ),
            "content_sha256": projection["content_sha256"],
            "declared_content_sha256": projection["declared_content_sha256"],
            "actual_paths": projection["actual_paths"],
            "actual_path_count": projection["actual_path_count"],
            "actual_paths_sha256": projection["actual_paths_sha256"],
            "actual_path_sample": projection["actual_path_sample"],
            "summary": summary,
            "validation_evidence": validation_evidence,
            "completed_at": completed_at,
            "terminal_event": terminal_event,
        }
        if projection_mode == "git-tree":
            result_fields["result_tree"] = projection["expected_index_tree"]
        else:
            for field in (
                "workspace_bytes_sha256",
                "workspace_file_count",
                "workspace_missing_path_count",
                "workspace_total_bytes",
            ):
                result_fields[field] = projection[field]
        result = materialized(result_fields)
        claim.update(
            {
                "status": "completing",
                "completion_result": result,
                "terminal_event": terminal_event,
                "completion_started_at": completed_at,
            }
        )
        replace_json(claim_path, claim, base=plane.state_root)
        return _finish_completion(plane, claim_path, claim, result, terminal_event)
