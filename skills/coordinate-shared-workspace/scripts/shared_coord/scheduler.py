"""Durable contention queue with overlap-local FIFO and recoverable grants."""

from __future__ import annotations

import copy
import shutil
import time
from pathlib import Path

from . import git_backend as git
from .activation import (
    activate_transaction_group,
    assert_scope_dependencies_acyclic,
    dependency_issues,
    load_claims,
    resolve_scope_dependency,
)
from .arbitration import claim_intent, record_paths
from .event_contract import request_event_details
from .recovery import active_groups, group_declared_paths
from .state import (
    EXCLUSIVE_INTENTS,
    TRANSACTION_MODES,
    active_claims,
    active_transactions,
    claim_path,
    crash_if_testing,
    emit_event,
    now,
    paths_overlap,
    read_json,
    replace_json,
    string_list,
    transaction_is_committed,
    validate_slug,
    write_json_exclusive,
)


REQUEST_MODES = TRANSACTION_MODES | {"exclusive", "wait"}
LIVE_REQUEST_STATES = {
    "activating",
    "blocked",
    "needs-attention",
    "queued",
    "ready",
}
CLAIM_SNAPSHOT_FIELDS = (
    "depends_on",
    "intent",
    "owner",
    "paths",
    "scope",
    "semantic_writes",
    "sensitive_to",
)
CLAIM_LIST_FIELDS = {"depends_on", "paths", "semantic_writes", "sensitive_to"}


def make_request_id() -> str:
    return validate_slug(f"request-{time.time_ns():x}"[-63:], "request id")


def request_path(location: Path, request_id: str) -> Path:
    return (
        location
        / "waiting"
        / "active"
        / f"{validate_slug(request_id, 'request id')}.json"
    )


def active_requests(location: Path) -> list[tuple[Path, dict[str, object]]]:
    return sorted(
        (
            (path, read_json(path))
            for path in (location / "waiting" / "active").glob("*.json")
        ),
        key=lambda item: (int(item[1].get("sequence", 0)), item[0].name),
    )


def _request_claims(request: dict[str, object]) -> list[dict[str, object]]:
    value = request.get("claim_snapshots")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("scheduling request claim snapshots are malformed")
    return value


def _request_paths(request: dict[str, object]) -> list[str]:
    return string_list(request, "paths")


def _requests_overlap(left: dict[str, object], right: dict[str, object]) -> bool:
    return any(
        paths_overlap(left_path, right_path)
        for left_path in _request_paths(left)
        for right_path in _request_paths(right)
    )


def _snapshot_claim(claim: dict[str, object]) -> dict[str, object]:
    return {
        field: copy.deepcopy(claim.get(field, [] if field in CLAIM_LIST_FIELDS else ""))
        for field in CLAIM_SNAPSHOT_FIELDS
    }


def create_request(
    location: Path,
    scopes: list[str],
    mode: str,
    steward: str,
    reason: str,
    *,
    contention_id: str | None = None,
    coordinator_epoch: int | None = None,
) -> tuple[Path, dict[str, object]]:
    if mode not in REQUEST_MODES:
        raise ValueError(f"request mode must be one of {', '.join(sorted(REQUEST_MODES))}")
    normalized_scopes: list[str] = []
    for scope in scopes:
        value = validate_slug(scope, "scope")
        if value not in normalized_scopes:
            normalized_scopes.append(value)
    if mode in {"exclusive", "wait"} and len(normalized_scopes) != 1:
        raise ValueError(f"a {mode} request requires exactly one scope")
    if mode in TRANSACTION_MODES and len(normalized_scopes) < 2:
        raise ValueError("a transaction scheduling request requires at least two scopes")
    claims_with_paths = load_claims(location, normalized_scopes)
    claims = [record for _, record in claims_with_paths]
    if mode == "exclusive" and claim_intent(claims[0]) not in EXCLUSIVE_INTENTS:
        raise ValueError("exclusive scheduling requires an exclusive semantic intent")
    if mode == "exclusive" and claims[0].get("status") != "pending-arbitration":
        raise ValueError(
            "exclusive scheduling is only needed for a pending-arbitration claim"
        )
    if mode == "wait" and claims[0].get("status") != "pending-arbitration":
        raise ValueError("wait scheduling requires a pending-arbitration claim")
    if mode in TRANSACTION_MODES:
        assert_scope_dependencies_acyclic(claims, mode)
    for _, existing in active_requests(location):
        shared = sorted(set(normalized_scopes) & set(string_list(existing, "scopes")))
        if shared:
            raise ValueError(
                f"scopes already belong to request {existing.get('request_id')!r}: "
                + ", ".join(shared)
            )

    sequence = time.time_ns()
    request_id = make_request_id()
    paths = sorted({path for claim in claims for path in record_paths(claim)})
    semantic_resources = sorted(
        {
            resource
            for claim in claims
            for key in ("semantic_writes", "sensitive_to")
            for resource in string_list(claim, key)
        }
    )
    record: dict[str, object] = {
        "schema": 1,
        "request_id": request_id,
        "sequence": sequence,
        "mode": mode,
        "steward": steward,
        "reason": reason,
        "scopes": normalized_scopes,
        "owners": sorted({str(claim["owner"]) for claim in claims}),
        "paths": paths,
        "semantic_resources": semantic_resources,
        "claim_snapshots": [_snapshot_claim(claim) for claim in claims],
        "status": "queued",
        "created_at": now(),
    }
    if contention_id is not None:
        record["contention_id"] = validate_slug(contention_id, "contention id")
    if coordinator_epoch is not None:
        if coordinator_epoch < 1:
            raise ValueError("coordinator epoch must be positive")
        record["coordinator_epoch"] = coordinator_epoch
    path = request_path(location, request_id)
    write_json_exclusive(path, record)
    emit_event(
        location,
        "queue-requested",
        None,
        request_event_details(record),
    )
    crash_if_testing("queue-requested")
    return path, record


def _snapshot_issues(location: Path, request: dict[str, object]) -> list[str]:
    issues: list[str] = []
    for snapshot in _request_claims(request):
        scope = str(snapshot.get("scope", ""))
        path = claim_path(location, scope)
        if not path.exists():
            issues.append(f"claim {scope!r} is missing")
            continue
        current = read_json(path)
        for field in CLAIM_SNAPSHOT_FIELDS:
            default: object = [] if field in CLAIM_LIST_FIELDS else ""
            expected = snapshot.get(field, default)
            actual = current.get(field, default)
            if actual != expected:
                issues.append(f"claim {scope!r} changed field {field!r}")
        if current.get("status", "active") not in {"active", "pending-arbitration"}:
            issues.append(
                f"claim {scope!r} advanced to {current.get('status')!r}"
            )
    return sorted(set(issues))


def _dependency_blockers(
    location: Path,
    request: dict[str, object],
) -> list[str]:
    claims = _request_claims(request)
    blockers = dependency_issues(location, claims)
    if request.get("mode") != "exclusive":
        return blockers
    selected = {str(claim["scope"]) for claim in claims}
    for claim in claims:
        for dependency in string_list(claim, "depends_on"):
            if dependency in selected:
                continue
            kind, transaction_id = resolve_scope_dependency(location, dependency)
            if kind == "transaction" and transaction_id is not None:
                if not transaction_is_committed(location, transaction_id):
                    blockers.append(
                        f"dependency scope {dependency!r} has not published yet"
                    )
    return sorted(set(blockers))


def _work_blockers(
    root: Path,
    location: Path,
    request: dict[str, object],
    earlier: list[dict[str, object]],
) -> tuple[list[str], list[dict[str, object]]]:
    paths = _request_paths(request)
    scopes = set(string_list(request, "scopes"))
    blockers = _dependency_blockers(location, request)
    references: list[dict[str, object]] = [
        {"kind": "dependency", "description": blocker}
        for blocker in blockers
    ]
    for prior in earlier:
        if _requests_overlap(prior, request):
            blockers.append(f"earlier overlapping request {prior['request_id']}")
            references.append(
                {
                    "kind": "request",
                    "request_id": prior["request_id"],
                    "owners": prior.get("owners", []),
                    "scopes": prior.get("scopes", []),
                }
            )
            break
    for _, claim in active_claims(location):
        scope = str(claim.get("scope", ""))
        if scope in scopes or claim.get("status", "active") not in {"active", "paused"}:
            continue
        if any(
            paths_overlap(requested, current)
            for requested in paths
            for current in record_paths(claim)
        ):
            blockers.append(f"active claim {scope}")
            references.append(
                {
                    "kind": "claim",
                    "scope": scope,
                    "owner": claim.get("owner"),
                }
            )
    for _, transaction in active_transactions(location):
        if any(
            paths_overlap(requested, current)
            for requested in paths
            for current in record_paths(transaction)
        ):
            blockers.append(f"active transaction {transaction['transaction_id']}")
            references.append(
                {
                    "kind": "transaction",
                    "transaction_id": transaction["transaction_id"],
                    "scope": transaction.get("scope"),
                    "owner": transaction.get("owner"),
                }
            )
    for _, group in active_groups(location):
        if any(
            paths_overlap(requested, current)
            for requested in paths
            for current in group_declared_paths(group)
        ):
            blockers.append(f"active group {group['group_id']}")
            members = group.get("members", [])
            references.append(
                {
                    "kind": "group",
                    "group_id": group["group_id"],
                    "scopes": sorted(
                        {
                            str(member["scope"])
                            for member in members
                            if isinstance(member, dict)
                            and isinstance(member.get("scope"), str)
                        }
                    ),
                    "owners": sorted(
                        {
                            str(member["owner"])
                            for member in members
                            if isinstance(member, dict)
                            and isinstance(member.get("owner"), str)
                        }
                    ),
                }
            )
    for dirty in git.status_paths(root):
        if any(paths_overlap(dirty, requested) for requested in paths):
            blockers.append(f"dirty canonical path {dirty}")
            references.append({"kind": "canonical-path", "path": dirty})
    references.sort(
        key=lambda reference: (
            str(reference.get("kind", "")),
            str(
                reference.get(
                    "request_id",
                    reference.get(
                        "transaction_id",
                        reference.get(
                            "group_id",
                            reference.get("scope", reference.get("path", "")),
                        ),
                    ),
                )
            ),
        )
    )
    return sorted(set(blockers)), references


def _transition(
    location: Path,
    path: Path,
    request: dict[str, object],
    status: str,
    blockers: list[str] | None = None,
    blocker_refs: list[dict[str, object]] | None = None,
) -> dict[str, object] | None:
    blockers = blockers or []
    blocker_refs = blocker_refs or []
    prior_status = request.get("status")
    prior_blockers = request.get("blockers", [])
    prior_refs = request.get("blocker_refs", [])
    if (
        prior_status == status
        and prior_blockers == blockers
        and prior_refs == blocker_refs
    ):
        return None
    request["status"] = status
    request["updated_at"] = now()
    if blockers:
        request["blockers"] = blockers
        request["blocker_refs"] = blocker_refs
    else:
        request.pop("blockers", None)
        request.pop("blocker_refs", None)
    replace_json(path, request)
    event = f"queue-{status}"
    emit_event(
        location,
        event,
        None,
        request_event_details(
            request,
            blockers=blockers,
            blocker_refs=blocker_refs,
        ),
    )
    return {
        "kind": "request",
        "request_id": request["request_id"],
        "action": status,
        "blockers": blockers,
    }


def _find_group_for_request(
    location: Path,
    request_id: str,
) -> tuple[Path, dict[str, object]] | None:
    for directory in (location / "groups" / "active", location / "groups" / "archive"):
        for path in sorted(directory.glob("*.json")):
            group = read_json(path)
            if group.get("request_id") == request_id:
                return path, group
    return None


def _archive_request(
    location: Path,
    path: Path,
    request: dict[str, object],
    status: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    request["status"] = status
    request[f"{status}_at"] = now()
    wait_ms = max(0, (time.time_ns() - int(request["sequence"])) // 1_000_000)
    request["wait_duration_ms"] = wait_ms
    if details:
        request.update(details)
    replace_json(path, request)
    archive = location / "waiting" / "archive" / f"{time.time_ns()}-{path.name}"
    shutil.move(path, archive)
    emit_event(
        location,
        f"queue-{status}",
        None,
        request_event_details(
            request,
            wait_duration_ms=wait_ms,
            archive=str(archive),
        ),
    )
    return {
        "kind": "request",
        "request_id": request["request_id"],
        "action": status,
        "archive": str(archive),
        "wait_duration_ms": wait_ms,
    }


def reconcile_request_activations(
    location: Path,
) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for path, request in active_requests(location):
        if request.get("status") != "activating":
            continue
        request_id = str(request["request_id"])
        mode = str(request["mode"])
        group = _find_group_for_request(location, request_id)
        if mode in TRANSACTION_MODES and group is not None:
            group_path, group_record = group
            updates.append(
                _archive_request(
                    location,
                    path,
                    request,
                    "activated",
                    {
                        "group_id": group_record.get("group_id"),
                        "group_record": str(group_path),
                    },
                )
            )
            continue
        if mode in {"exclusive", "wait"}:
            scope = string_list(request, "scopes")[0]
            claim = claim_path(location, scope)
            marker = f"{mode}_request_id"
            if claim.exists() and read_json(claim).get(marker) == request_id:
                updates.append(
                    _archive_request(
                        location,
                        path,
                        request,
                        "activated",
                        {f"{mode}_scope": scope},
                    )
                )
                continue
        update = _transition(location, path, request, "queued")
        if update is not None:
            update["action"] = "activation-retryable"
            updates.append(update)
    return updates


def refresh_queue_states(
    root: Path,
    location: Path,
) -> list[dict[str, object]]:
    updates = reconcile_request_activations(location)
    earlier: list[dict[str, object]] = []
    for path, request in active_requests(location):
        if request.get("status") == "activating":
            earlier.append(request)
            continue
        issues = _snapshot_issues(location, request)
        if issues:
            update = _transition(
                location,
                path,
                request,
                "needs-attention",
                issues,
                [
                    {"kind": "claim-snapshot", "description": issue}
                    for issue in issues
                ],
            )
        else:
            blockers, blocker_refs = _work_blockers(
                root, location, request, earlier
            )
            update = _transition(
                location,
                path,
                request,
                "blocked" if blockers else "ready",
                blockers,
                blocker_refs,
            )
        if update is not None:
            updates.append(update)
        earlier.append(request)
    return updates


def _activate_pending_claim(
    location: Path,
    request: dict[str, object],
) -> str:
    mode = str(request["mode"])
    scope = string_list(request, "scopes")[0]
    path = claim_path(location, scope)
    claim = read_json(path)
    if claim.get("status") != "pending-arbitration":
        raise ValueError(
            f"{mode} claim {scope!r} must be pending-arbitration before grant"
        )
    claim["status"] = "active"
    claim[f"{mode}_request_id"] = request["request_id"]
    claim[f"{mode}_granted_at"] = now()
    replace_json(path, claim)
    crash_if_testing(f"{mode}-claim-granted")
    return scope


def schedule_ready_requests(
    root: Path,
    location: Path,
    steward: str,
    canonical_branch: str,
    limit: int = 0,
    *,
    contention_only: bool = False,
) -> list[dict[str, object]]:
    updates = refresh_queue_states(root, location)
    activated = 0
    for path, request in list(active_requests(location)):
        if request.get("status") != "ready":
            continue
        if contention_only and not isinstance(request.get("contention_id"), str):
            continue
        if limit > 0 and activated >= limit:
            break
        if request.get("steward") != steward:
            update = _transition(
                location,
                path,
                request,
                "needs-attention",
                ["request belongs to a different steward"],
            )
            if update is not None:
                updates.append(update)
            continue
        request["status"] = "activating"
        request["activating_at"] = now()
        replace_json(path, request)
        emit_event(
            location,
            "queue-activating",
            None,
            request_event_details(request),
        )
        crash_if_testing("queue-activation-recorded")
        try:
            if request.get("mode") in {"exclusive", "wait"}:
                mode = str(request["mode"])
                scope = _activate_pending_claim(location, request)
                update = _archive_request(
                    location,
                    path,
                    request,
                    "activated",
                    {f"{mode}_scope": scope},
                )
            else:
                transactions = activate_transaction_group(
                    root,
                    location,
                    string_list(request, "scopes"),
                    str(request["mode"]),
                    steward,
                    canonical_branch,
                    str(request["reason"]),
                    request_id=str(request["request_id"]),
                )
                update = _archive_request(
                    location,
                    path,
                    request,
                    "activated",
                    {
                        "group_id": transactions[0].get("group_id"),
                        "transaction_ids": [
                            transaction["transaction_id"] for transaction in transactions
                        ],
                    },
                )
        except (OSError, ValueError, git.GitError, RuntimeError) as error:
            group = _find_group_for_request(location, str(request["request_id"]))
            if group is not None:
                group_path, group_record = group
                update = _archive_request(
                    location,
                    path,
                    request,
                    "activated",
                    {
                        "group_id": group_record.get("group_id"),
                        "group_record": str(group_path),
                        "activation_issue": str(error),
                    },
                )
            else:
                update = _transition(
                    location,
                    path,
                    request,
                    "needs-attention",
                    [str(error)],
                ) or {
                    "kind": "request",
                    "request_id": request["request_id"],
                    "action": "needs-attention",
                    "blockers": [str(error)],
                }
        updates.append(update)
        activated += 1
        updates.extend(refresh_queue_states(root, location))
    return updates


def cancel_request(
    location: Path,
    request_id: str,
    steward: str,
    owners: set[str],
    reason: str,
) -> dict[str, object]:
    path = request_path(location, request_id)
    if not path.exists():
        raise ValueError(f"active scheduling request does not exist: {request_id}")
    request = read_json(path)
    if request.get("status") == "activating":
        raise ValueError("an activating request must be reconciled before cancellation")
    if request.get("steward") != steward:
        raise ValueError("scheduling request belongs to a different steward")
    expected = set(string_list(request, "owners"))
    if owners != expected:
        raise ValueError(
            "request cancellation requires exact owner acknowledgements: "
            + ", ".join(sorted(expected))
        )
    return _archive_request(
        location,
        path,
        request,
        "cancelled",
        {"cancel_reason": reason, "cancelled_by": sorted(owners)},
    )


def assert_manual_activation_allowed(
    location: Path,
    scopes: list[str],
) -> None:
    claims = [record for _, record in load_claims(location, scopes)]
    paths = sorted({path for claim in claims for path in record_paths(claim)})
    for _, request in active_requests(location):
        if request.get("status") not in LIVE_REQUEST_STATES:
            continue
        if any(
            paths_overlap(requested, queued)
            for requested in paths
            for queued in _request_paths(request)
        ):
            raise ValueError(
                f"manual activation would bypass queued request "
                f"{request.get('request_id')!r}; run tx schedule"
            )
