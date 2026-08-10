"""Contention-local coordination leases, decisions, and cooperative progress."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time
from pathlib import Path

from . import git_backend as git
from .arbitration import claim_intent, recommend_decision, record_paths
from .contention_lease import (
    DEFAULT_LEASE_SECONDS,
    assert_coordinator,
    lease_expired,
    new_lease,
    validate_lease_seconds,
)
from .contention_store import (
    active_contentions,
    contention_path,
    make_contention_id,
    participant_owners,
    participant_scopes,
    read_contention,
)
from .scheduler import (
    REQUEST_MODES,
    active_requests,
    create_request,
    schedule_ready_requests,
)
from .state import (
    EXCLUSIVE_INTENTS,
    active_claims,
    crash_if_testing,
    emit_event,
    now,
    paths_overlap,
    read_json,
    replace_json,
    require_text,
    string_list,
    validate_slug,
    write_json_exclusive,
)


DECISIONS = REQUEST_MODES | {"handoff"}
COORDINATABLE_STATES = {
    "awaiting-acks",
    "needs-decision",
    "open",
    "ready",
    "scheduled",
}
CLAIM_DIGEST_FIELDS = (
    "depends_on",
    "intent",
    "owner",
    "paths",
    "scope",
    "semantic_writes",
    "sensitive_to",
    "status",
)
CLAIM_LIST_FIELDS = {"depends_on", "paths", "semantic_writes", "sensitive_to"}


def _claim_snapshot(claim: dict[str, object]) -> dict[str, object]:
    return {
        field: copy.deepcopy(
            claim.get(field, [] if field in CLAIM_LIST_FIELDS else "")
        )
        for field in CLAIM_DIGEST_FIELDS
    }


def _claim_digest(claim: dict[str, object]) -> str:
    payload = json.dumps(
        _claim_snapshot(claim),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _participant(claim: dict[str, object]) -> dict[str, object]:
    return {
        "scope": str(claim["scope"]),
        "owner": str(claim["owner"]),
        "paths": record_paths(claim),
        "intent": claim_intent(claim),
        "semantic_writes": string_list(claim, "semantic_writes"),
        "sensitive_to": string_list(claim, "sensitive_to"),
        "claim_digest": _claim_digest(claim),
    }


def _live_claims_for_scopes(
    location: Path,
    scopes: list[str],
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    for scope in scopes:
        path = location / "claims" / f"{validate_slug(scope, 'scope')}.json"
        if not path.exists():
            raise ValueError(f"contention claim no longer exists: {scope}")
        claims.append(read_json(path))
    return claims


def _claim_sets_overlap(left: dict[str, object], right: dict[str, object]) -> bool:
    return any(
        paths_overlap(left_path, right_path)
        for left_path in record_paths(left)
        for right_path in record_paths(right)
    )


def _contention_resources(claims: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    paths = sorted({path for claim in claims for path in record_paths(claim)})
    semantics = sorted(
        {
            resource
            for claim in claims
            for key in ("semantic_writes", "sensitive_to")
            for resource in string_list(claim, key)
        }
    )
    return paths, semantics


def _find_joinable_contention(
    location: Path,
    scopes: set[str],
    paths: list[str],
) -> tuple[Path, dict[str, object]] | None:
    for path, record in active_contentions(location):
        if record.get("status") not in {
            "awaiting-acks",
            "needs-decision",
            "open",
            "ready",
        }:
            continue
        existing_scopes = set(participant_scopes(record))
        existing_paths = string_list(record, "paths")
        if scopes & existing_scopes or any(
            paths_overlap(left, right) for left in paths for right in existing_paths
        ):
            return path, record
    return None


def open_or_join_contention(
    root: Path,
    location: Path,
    trigger_scope: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, object] | None:
    """Create or extend a contention after a pending claim is durably recorded."""
    trigger_scope = validate_slug(trigger_scope, "scope")
    lease_seconds = validate_lease_seconds(lease_seconds)
    trigger_path = location / "claims" / f"{trigger_scope}.json"
    if not trigger_path.exists():
        return None
    trigger = read_json(trigger_path)
    if trigger.get("status") != "pending-arbitration":
        return None
    for _, record in active_contentions(location):
        if trigger_scope in participant_scopes(record):
            return record
    overlapping = [
        claim
        for _, claim in active_claims(location)
        if claim.get("scope") != trigger_scope
        and claim.get("status", "active") in {"active", "pending-arbitration"}
        and _claim_sets_overlap(trigger, claim)
    ]
    if not overlapping:
        return None
    claims_by_scope = {
        str(claim["scope"]): claim for claim in [trigger, *overlapping]
    }
    paths, semantics = _contention_resources(list(claims_by_scope.values()))
    existing = _find_joinable_contention(location, set(claims_by_scope), paths)
    if existing is not None:
        path, record = existing
        for scope in participant_scopes(record):
            claim_path = location / "claims" / f"{scope}.json"
            if claim_path.exists():
                claims_by_scope[scope] = read_json(claim_path)
        prior_scopes = set(participant_scopes(record))
        new_scopes = set(claims_by_scope)
        if prior_scopes == new_scopes:
            return record
        claims = [claims_by_scope[scope] for scope in sorted(new_scopes)]
        paths, semantics = _contention_resources(claims)
        record["scopes"] = sorted(new_scopes)
        record["participants"] = [_participant(claim) for claim in claims]
        record["paths"] = paths
        record["semantic_resources"] = semantics
        record["participant_revision"] = int(record.get("participant_revision", 1)) + 1
        record["status"] = "open"
        record["updated_at"] = now()
        invalidated_revision = None
        if isinstance(record.get("decision"), dict):
            invalidated_revision = record["decision"].get("revision")
            record.pop("decision", None)
            record.pop("responses", None)
        replace_json(path, record)
        emit_event(
            location,
            "contention-participant-joined",
            None,
            {
                "contention_id": record["contention_id"],
                "scope": trigger_scope,
                "owner": trigger["owner"],
                "scopes": record["scopes"],
                "paths": paths,
                "semantic_resources": semantics,
                "invalidated_decision_revision": invalidated_revision,
            },
        )
        return record

    contention_id = make_contention_id()
    claims = [claims_by_scope[scope] for scope in sorted(claims_by_scope)]
    sequence = time.time_ns()
    recommendation = recommend_decision(claims, git.status_paths(root))
    record = {
        "schema": 1,
        "contention_id": contention_id,
        "sequence": sequence,
        "status": "open",
        "trigger_scope": trigger_scope,
        "scopes": sorted(claims_by_scope),
        "participants": [_participant(claim) for claim in claims],
        "participant_revision": 1,
        "paths": paths,
        "semantic_resources": semantics,
        "coordinator": new_lease(str(trigger["owner"]), 1, lease_seconds),
        "recommendation": recommendation,
        "created_at": now(),
    }
    path = contention_path(location, contention_id)
    write_json_exclusive(path, record)
    emit_event(
        location,
        "contention-opened",
        None,
        {
            "contention_id": contention_id,
            "scope": trigger_scope,
            "owner": trigger["owner"],
            "coordinator": trigger["owner"],
            "coordinator_epoch": 1,
            "scopes": record["scopes"],
            "owners": participant_owners(record),
            "paths": paths,
            "semantic_resources": semantics,
            "recommendation": recommendation["recommendation"],
            "reason": recommendation["reason"],
        },
    )
    return record


def discover_contentions(root: Path, location: Path) -> list[str]:
    discovered: list[str] = []
    pending = sorted(
        (
            claim
            for _, claim in active_claims(location)
            if claim.get("status") == "pending-arbitration"
        ),
        key=lambda claim: str(claim.get("created_at", "")),
    )
    for claim in pending:
        record = open_or_join_contention(root, location, str(claim["scope"]))
        if record is not None and str(record["contention_id"]) not in discovered:
            discovered.append(str(record["contention_id"]))
    return discovered


def _default_target_scopes(
    claims: list[dict[str, object]],
    decision: str,
) -> list[str]:
    if decision in {"parallel-tx", "ordered-tx"}:
        return sorted(str(claim["scope"]) for claim in claims)
    pending = sorted(
        str(claim["scope"])
        for claim in claims
        if claim.get("status") == "pending-arbitration"
    )
    if len(pending) != 1:
        raise ValueError(
            f"{decision} requires one explicit target scope when multiple claims are pending"
        )
    return pending


def _validate_decision(
    claims: list[dict[str, object]],
    decision: str,
    target_scopes: list[str],
) -> list[str]:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(sorted(DECISIONS))}")
    available = {str(claim["scope"]): claim for claim in claims}
    normalized: list[str] = []
    for scope in target_scopes:
        scope = validate_slug(scope, "target scope")
        if scope not in available:
            raise ValueError(f"target scope is not in the contention: {scope}")
        if scope not in normalized:
            normalized.append(scope)
    if decision in {"parallel-tx", "ordered-tx"} and len(normalized) < 2:
        raise ValueError("a transaction decision requires at least two target scopes")
    if decision in {"exclusive", "handoff", "wait"} and len(normalized) != 1:
        raise ValueError(f"{decision} requires exactly one target scope")
    target = available[normalized[0]] if len(normalized) == 1 else None
    if decision == "exclusive" and target is not None:
        if claim_intent(target) not in EXCLUSIVE_INTENTS:
            raise ValueError("exclusive requires a contract/refactor/move/delete/generated target")
    if decision in {"exclusive", "wait"} and target is not None:
        if target.get("status") != "pending-arbitration":
            raise ValueError(f"{decision} target must still be pending arbitration")
    return normalized


def propose_decision(
    root: Path,
    location: Path,
    contention_id: str,
    owner: str,
    epoch: int,
    decision: str | None,
    target_scopes: list[str],
    reason: str,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    assert_coordinator(record, owner, epoch)
    if record.get("status") not in COORDINATABLE_STATES:
        raise ValueError(f"contention cannot accept a decision in {record.get('status')!r}")
    claims = _live_claims_for_scopes(location, participant_scopes(record))
    recommendation = recommend_decision(claims, git.status_paths(root))
    if decision is None:
        recommended = str(recommendation["recommendation"])
        if recommended not in REQUEST_MODES:
            raise ValueError(
                f"recommendation {recommended!r} requires an explicit semantic decision"
            )
        decision = recommended
    targets = _validate_decision(
        claims,
        decision,
        target_scopes or _default_target_scopes(claims, decision),
    )
    prior = record.get("decision")
    prior_revision = int(prior.get("revision", 0)) if isinstance(prior, dict) else 0
    revision = prior_revision + 1
    proposal = {
        "revision": revision,
        "mode": decision,
        "target_scopes": targets,
        "reason": require_text(reason, "decision reason"),
        "proposed_by": owner,
        "proposed_at": now(),
        "coordinator_epoch": epoch,
        "claim_digests": {
            str(claim["scope"]): _claim_digest(claim) for claim in claims
        },
        "recommendation": recommendation,
    }
    owners = participant_owners(record)
    record["decision"] = proposal
    record["responses"] = {
        owner: {
            "accepted": True,
            "at": now(),
            "reason": "coordinator proposed this revision",
        }
    }
    record["status"] = "ready" if owners == [owner] else "awaiting-acks"
    record["updated_at"] = now()
    replace_json(path, record)
    emit_event(
        location,
        "contention-decision-proposed",
        None,
        {
            "contention_id": contention_id,
            "coordinator": owner,
            "coordinator_epoch": epoch,
            "decision_revision": revision,
            "mode": decision,
            "target_scopes": targets,
            "scopes": record["scopes"],
            "owners": owners,
            "paths": record["paths"],
            "semantic_resources": record["semantic_resources"],
            "reason": proposal["reason"],
            "recommendation": recommendation["recommendation"],
        },
    )
    return record


def respond_to_decision(
    location: Path,
    contention_id: str,
    owner: str,
    revision: int,
    accepted: bool,
    reason: str,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    owner = validate_slug(owner, "owner")
    if owner not in participant_owners(record):
        raise ValueError("only a contention participant may respond")
    decision = record.get("decision")
    if not isinstance(decision, dict) or decision.get("revision") != revision:
        raise ValueError("decision revision changed; inspect before responding")
    responses = record.get("responses")
    if not isinstance(responses, dict):
        responses = {}
    responses[owner] = {
        "accepted": accepted,
        "at": now(),
        "reason": reason.strip(),
    }
    record["responses"] = responses
    owners = participant_owners(record)
    rejected = sorted(
        participant
        for participant in owners
        if isinstance(responses.get(participant), dict)
        and responses[participant].get("accepted") is False
    )
    accepted_owners = sorted(
        participant
        for participant in owners
        if isinstance(responses.get(participant), dict)
        and responses[participant].get("accepted") is True
    )
    record["status"] = (
        "needs-decision"
        if rejected
        else "ready" if accepted_owners == owners else "awaiting-acks"
    )
    record["updated_at"] = now()
    replace_json(path, record)
    emit_event(
        location,
        "contention-decision-accepted" if accepted else "contention-decision-rejected",
        None,
        {
            "contention_id": contention_id,
            "owner": owner,
            "decision_revision": revision,
            "mode": decision["mode"],
            "reason": reason.strip(),
            "paths": record["paths"],
            "semantic_resources": record["semantic_resources"],
        },
    )
    return record


def _decision_digest_issues(
    location: Path,
    record: dict[str, object],
) -> list[str]:
    decision = record.get("decision")
    if not isinstance(decision, dict) or not isinstance(
        decision.get("claim_digests"), dict
    ):
        return ["contention decision snapshot is malformed"]
    expected = decision["claim_digests"]
    issues: list[str] = []
    for scope in participant_scopes(record):
        path = location / "claims" / f"{scope}.json"
        if not path.exists():
            issues.append(f"claim {scope!r} is missing")
            continue
        if expected.get(scope) != _claim_digest(read_json(path)):
            issues.append(f"claim {scope!r} changed after the decision")
    return issues


def _request_record(
    location: Path,
    request_id: str,
) -> tuple[Path, dict[str, object]] | None:
    for path, record in active_requests(location):
        if record.get("request_id") == request_id:
            return path, record
    for path in sorted((location / "waiting" / "archive").glob("*.json")):
        record = read_json(path)
        if record.get("request_id") == request_id:
            return path, record
    return None


def _request_for_contention(
    location: Path,
    contention_id: str,
) -> tuple[Path, dict[str, object]] | None:
    for path, record in active_requests(location):
        if record.get("contention_id") == contention_id:
            return path, record
    for path in sorted((location / "waiting" / "archive").glob("*.json")):
        record = read_json(path)
        if record.get("contention_id") == contention_id:
            return path, record
    return None


def _archive_completed_contention(
    location: Path,
    path: Path,
    record: dict[str, object],
    request_path_value: Path,
    request: dict[str, object],
) -> dict[str, object]:
    record["status"] = "completed"
    record["completed_at"] = now()
    record["request_status"] = request.get("status")
    record["request_archive"] = str(request_path_value)
    duration_ms = max(0, (time.time_ns() - int(record["sequence"])) // 1_000_000)
    record["coordination_duration_ms"] = duration_ms
    replace_json(path, record)
    archive = location / "contentions" / "archive" / f"{time.time_ns()}-{path.name}"
    shutil.move(path, archive)
    decision = record.get("decision", {})
    emit_event(
        location,
        "contention-completed",
        None,
        {
            "contention_id": record["contention_id"],
            "request_id": record.get("request_id"),
            "mode": decision.get("mode") if isinstance(decision, dict) else None,
            "decision_revision": (
                decision.get("revision") if isinstance(decision, dict) else None
            ),
            "scopes": record["scopes"],
            "owners": participant_owners(record),
            "paths": record["paths"],
            "semantic_resources": record["semantic_resources"],
            "coordination_duration_ms": duration_ms,
            "archive": str(archive),
        },
    )
    return {
        "contention_id": record["contention_id"],
        "action": "completed",
        "archive": str(archive),
        "coordination_duration_ms": duration_ms,
    }


def reconcile_contentions(location: Path) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for path, record in list(active_contentions(location)):
        request_id = record.get("request_id")
        if not isinstance(request_id, str):
            recovered = _request_for_contention(
                location,
                str(record["contention_id"]),
            )
            if recovered is None:
                continue
            _, recovered_request = recovered
            request_id = str(recovered_request["request_id"])
            record["request_id"] = request_id
            record["status"] = "scheduled"
            record["request_status"] = recovered_request.get("status")
            record["updated_at"] = now()
            replace_json(path, record)
            emit_event(
                location,
                "contention-request-linked",
                None,
                {
                    "contention_id": record["contention_id"],
                    "request_id": request_id,
                    "mode": recovered_request.get("mode"),
                    "paths": record["paths"],
                    "semantic_resources": record["semantic_resources"],
                    "recovered": True,
                },
            )
            updates.append(
                {
                    "contention_id": record["contention_id"],
                    "action": "request-linked",
                    "request_id": request_id,
                }
            )
        located = _request_record(location, request_id)
        if located is None:
            record["status"] = "needs-decision"
            record["issue"] = "linked scheduling request is missing"
            replace_json(path, record)
            updates.append(
                {
                    "contention_id": record["contention_id"],
                    "action": "needs-decision",
                    "issue": record["issue"],
                }
            )
            continue
        request_path_value, request = located
        request_status = request.get("status")
        if request_status == "activated":
            updates.append(
                _archive_completed_contention(
                    location,
                    path,
                    record,
                    request_path_value,
                    request,
                )
            )
        elif request_status == "cancelled":
            record["status"] = "needs-decision"
            record["issue"] = "linked scheduling request was cancelled"
            record.pop("request_id", None)
            replace_json(path, record)
            updates.append(
                {
                    "contention_id": record["contention_id"],
                    "action": "needs-decision",
                    "issue": record["issue"],
                }
            )
        else:
            next_status = "scheduled"
            if record.get("status") != next_status or record.get(
                "request_status"
            ) != request_status:
                record["status"] = next_status
                record["request_status"] = request_status
                record["updated_at"] = now()
                replace_json(path, record)
    return updates


def drive_ready_requests(root: Path, location: Path) -> list[dict[str, object]]:
    """Advance already-authorized requests when a participant releases a blocker."""
    steward_path = location / "steward.json"
    if not steward_path.exists():
        return []
    steward_record = read_json(steward_path)
    steward = steward_record.get("steward")
    canonical_branch = steward_record.get("canonical_branch")
    if not isinstance(steward, str) or not isinstance(canonical_branch, str):
        return []
    if git.current_branch(root) != canonical_branch:
        return []
    updates = schedule_ready_requests(
        root,
        location,
        steward,
        canonical_branch,
        contention_only=True,
    )
    updates.extend(reconcile_contentions(location))
    return updates


def enact_decision(
    root: Path,
    location: Path,
    contention_id: str,
    owner: str,
    epoch: int,
) -> dict[str, object]:
    path, record = read_contention(location, contention_id)
    assert_coordinator(record, owner, epoch)
    if record.get("status") not in {"ready", "scheduled"}:
        raise ValueError("contention requires unanimous acceptance before enactment")
    decision = record.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("contention has no decision")
    if decision.get("mode") == "handoff":
        raise ValueError(
            "handoff decisions remain owner-authorized; use the claim or transaction handoff workflow"
        )
    issues = _decision_digest_issues(location, record)
    if issues:
        record["status"] = "needs-decision"
        record["decision_issues"] = issues
        record["updated_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "contention-decision-invalidated",
            None,
            {
                "contention_id": contention_id,
                "coordinator": owner,
                "coordinator_epoch": epoch,
                "decision_revision": decision.get("revision"),
                "issues": issues,
                "paths": record["paths"],
                "semantic_resources": record["semantic_resources"],
            },
        )
        raise ValueError("contention decision became stale: " + "; ".join(issues))
    steward_path = location / "steward.json"
    if not steward_path.exists():
        raise ValueError("transaction coordinator is not initialized; run tx init")
    steward_record = read_json(steward_path)
    steward = validate_slug(str(steward_record.get("steward", "")), "steward")
    canonical_branch = str(steward_record.get("canonical_branch", ""))
    if git.current_branch(root) != canonical_branch:
        raise ValueError(
            f"canonical workspace is not on the recorded branch {canonical_branch!r}"
        )
    request_id = record.get("request_id")
    if not isinstance(request_id, str):
        recovered = _request_for_contention(location, contention_id)
        if recovered is None:
            _, request = create_request(
                location,
                list(decision["target_scopes"]),
                str(decision["mode"]),
                steward,
                str(decision["reason"]),
                contention_id=contention_id,
                coordinator_epoch=epoch,
            )
            crash_if_testing("contention-request-created")
        else:
            _, request = recovered
        request_id = str(request["request_id"])
        record["request_id"] = request_id
        record["status"] = "scheduled"
        record["request_status"] = request["status"]
        record["enacted_at"] = now()
        record["updated_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "contention-enacted",
            None,
            {
                "contention_id": contention_id,
                "request_id": request_id,
                "coordinator": owner,
                "coordinator_epoch": epoch,
                "decision_revision": decision["revision"],
                "mode": decision["mode"],
                "target_scopes": decision["target_scopes"],
                "paths": record["paths"],
                "semantic_resources": record["semantic_resources"],
            },
        )
    updates = schedule_ready_requests(root, location, steward, canonical_branch)
    contention_updates = reconcile_contentions(location)
    return {
        "contention_id": contention_id,
        "request_id": request_id,
        "queue": updates,
        "contentions": contention_updates,
    }


def contention_status(location: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for _, record in active_contentions(location):
        snapshot = copy.deepcopy(record)
        snapshot["coordinator_lease_expired"] = lease_expired(record)
        owners = participant_owners(record)
        responses = record.get("responses", {})
        if not isinstance(responses, dict):
            responses = {}
        snapshot["missing_responses"] = [
            owner for owner in owners if owner not in responses
        ]
        result.append(snapshot)
    return result
