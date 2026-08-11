"""Bounded identity fields for reconstructing collaboration traces."""

from __future__ import annotations

import copy
from typing import Iterable


TRACE_SCHEMA = 1


def _present(value: object) -> bool:
    return value is not None and value != "" and value != []


def _merge(
    identity: dict[str, object],
    details: dict[str, object],
) -> dict[str, object]:
    result = {
        key: copy.deepcopy(value)
        for key, value in identity.items()
        if _present(value)
    }
    result.update(
        {
            key: copy.deepcopy(value)
            for key, value in details.items()
            if value is not None
        }
    )
    return result


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _semantic_resources(record: dict[str, object]) -> list[str]:
    direct = _strings(record.get("semantic_resources"))
    if direct:
        return direct
    return sorted(
        set(_strings(record.get("semantic_writes")))
        | set(_strings(record.get("sensitive_to")))
    )


def request_event_details(
    request: dict[str, object],
    **details: object,
) -> dict[str, object]:
    """Return the stable queue identity carried by every request event."""
    return _merge(
        {
            "trace_schema": TRACE_SCHEMA,
            "request_id": request.get("request_id"),
            "mode": request.get("mode"),
            "request_status": request.get("status"),
            "scopes": _strings(request.get("scopes")),
            "owners": _strings(request.get("owners")),
            "paths": _strings(request.get("paths")),
            "semantic_resources": _semantic_resources(request),
            "actor_owner": request.get("steward"),
            "steward": request.get("steward"),
            "reason": request.get("reason"),
            "contention_id": request.get("contention_id"),
            "coordinator_epoch": request.get("coordinator_epoch"),
            "blocker_refs": request.get("blocker_refs"),
        },
        details,
    )


def transaction_event_details(
    transaction: dict[str, object],
    *,
    actor_owner: str | None = None,
    work_owner: str | None = None,
    **details: object,
) -> dict[str, object]:
    """Return enough transaction identity to draw a fork and later rejoin."""
    resolved_owner = work_owner or (
        str(transaction["owner"])
        if isinstance(transaction.get("owner"), str)
        else None
    )
    scope = transaction.get("scope")
    mode = transaction.get("decision", transaction.get("mode"))
    return _merge(
        {
            "trace_schema": TRACE_SCHEMA,
            "owner": resolved_owner,
            "work_owner": resolved_owner,
            "actor_owner": actor_owner or resolved_owner,
            "scope": scope,
            "scopes": [scope] if isinstance(scope, str) else [],
            "owners": [resolved_owner] if resolved_owner is not None else [],
            "group_id": transaction.get("group_id"),
            "request_id": transaction.get("request_id"),
            "mode": mode,
            "transaction_status": transaction.get("status"),
            "branch": transaction.get("branch"),
            "base_revision": transaction.get("base_revision"),
            "canonical_branch": transaction.get("canonical_branch"),
            "paths": _strings(transaction.get("paths")),
            "semantic_resources": _semantic_resources(transaction),
            "steward": transaction.get("steward"),
        },
        details,
    )


def _group_transactions(group: dict[str, object]) -> list[dict[str, object]]:
    members = group.get("members")
    if not isinstance(members, list):
        return []
    transactions: list[dict[str, object]] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        planned = member.get("planned_transaction")
        if isinstance(planned, dict):
            transactions.append(planned)
    return transactions


def _aggregate_strings(
    records: Iterable[dict[str, object]],
    key: str,
) -> list[str]:
    return sorted(
        {
            value
            for record in records
            for value in _strings(record.get(key))
        }
    )


def group_event_details(
    group: dict[str, object],
    **details: object,
) -> dict[str, object]:
    """Return the shared identity for transaction-group lifecycle events."""
    transactions = _group_transactions(group)
    scopes = sorted(
        {
            str(transaction["scope"])
            for transaction in transactions
            if isinstance(transaction.get("scope"), str)
        }
    )
    owners = sorted(
        {
            str(transaction["owner"])
            for transaction in transactions
            if isinstance(transaction.get("owner"), str)
        }
    )
    semantic_resources = sorted(
        {
            resource
            for transaction in transactions
            for resource in _semantic_resources(transaction)
        }
    )
    return _merge(
        {
            "trace_schema": TRACE_SCHEMA,
            "group_id": group.get("group_id"),
            "request_id": group.get("request_id"),
            "mode": group.get("mode"),
            "group_status": group.get("status"),
            "scopes": scopes,
            "owners": owners,
            "transactions": [
                transaction["transaction_id"]
                for transaction in transactions
                if isinstance(transaction.get("transaction_id"), str)
            ],
            "paths": _aggregate_strings(transactions, "paths"),
            "semantic_resources": semantic_resources,
            "actor_owner": group.get("steward"),
            "steward": group.get("steward"),
            "canonical_branch": group.get("canonical_branch"),
            "base_revision": group.get("base_revision"),
        },
        details,
    )


def work_event_details(
    work: dict[str, object],
    **details: object,
) -> dict[str, object]:
    """Return owner and dependency identity for diagnostic work disposition."""
    owner = work.get("owner")
    scope = work.get("scope")
    return _merge(
        {
            "trace_schema": TRACE_SCHEMA,
            "work_state_id": work.get("work_state_id"),
            "owner": owner,
            "work_owner": owner,
            "actor_owner": owner,
            "scope": scope,
            "scopes": [scope] if isinstance(scope, str) else [],
            "owners": [owner] if isinstance(owner, str) else [],
            "disposition": work.get("disposition"),
            "reason": work.get("reason"),
            "request_id": work.get("request_id"),
            "contention_id": work.get("contention_id"),
            "transaction_id": work.get("transaction_id"),
            "run_id": work.get("run_id"),
            "alternate_scope": work.get("alternate_scope"),
            "alternate_run_id": work.get("alternate_run_id"),
            "blocked_by_owners": work.get("blocked_by_owners"),
            "blocked_by_scopes": work.get("blocked_by_scopes"),
            "paths": work.get("paths"),
            "semantic_resources": work.get("semantic_resources"),
        },
        details,
    )
