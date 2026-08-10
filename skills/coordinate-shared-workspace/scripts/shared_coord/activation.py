"""Atomic transaction-group activation and publish dependency validation."""

from __future__ import annotations

import time
from pathlib import Path

from . import git_backend as git
from .arbitration import claim_intent, record_paths
from .recovery import (
    active_groups,
    create_group,
    group_declared_paths,
    materialize_group,
)
from .state import (
    TRANSACTION_MODES,
    active_claims,
    active_transactions,
    claim_path,
    overlapping_pairs,
    now,
    paths_overlap,
    read_json,
    string_list,
    validate_slug,
)


def load_claims(
    location: Path,
    scopes: list[str],
) -> list[tuple[Path, dict[str, object]]]:
    loaded: list[tuple[Path, dict[str, object]]] = []
    for scope in scopes:
        path = claim_path(location, validate_slug(scope, "scope"))
        if not path.exists():
            raise ValueError(f"active claim does not exist: {scope}")
        record = read_json(path)
        if record.get("status", "active") not in {"active", "pending-arbitration"}:
            raise ValueError(f"claim {scope!r} is not eligible for arbitration")
        loaded.append((path, record))
    return loaded


def make_transaction_id(scope: str) -> str:
    suffix = f"{time.time_ns():x}"[-10:]
    prefix = scope[:51].rstrip("-")
    return validate_slug(f"{prefix}-{suffix}", "transaction id")


def _planned_group_transactions(location: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for _, group in active_groups(location):
        members = group.get("members", [])
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            planned = member.get("planned_transaction")
            if isinstance(planned, dict):
                records.append(planned)
    return records


def _archived_transactions(location: Path) -> list[dict[str, object]]:
    return [
        read_json(path)
        for path in sorted((location / "transactions" / "archive").glob("*.json"))
    ]


def _released_direct_scope_exists(location: Path, scope: str) -> bool:
    for path in sorted((location / "archive" / "claims").glob(f"*-{scope}.json")):
        record = read_json(path)
        if record.get("scope") == scope and record.get("released_at") is not None:
            return True
    return False


def resolve_scope_dependency(location: Path, scope: str) -> tuple[str, str | None]:
    for _, transaction in active_transactions(location):
        if transaction.get("scope") == scope:
            return "transaction", str(transaction["transaction_id"])
    for transaction in _planned_group_transactions(location):
        if transaction.get("scope") == scope:
            return "transaction", str(transaction["transaction_id"])
    committed = [
        transaction
        for transaction in _archived_transactions(location)
        if transaction.get("scope") == scope and transaction.get("status") == "committed"
    ]
    if committed:
        return "transaction", str(committed[-1]["transaction_id"])
    if _released_direct_scope_exists(location, scope):
        return "released", None
    return "missing", None


def dependency_issues(
    location: Path,
    claims: list[dict[str, object]],
) -> list[str]:
    selected = {str(claim["scope"]) for claim in claims}
    issues: list[str] = []
    for claim in claims:
        for dependency in string_list(claim, "depends_on"):
            if dependency in selected:
                continue
            kind, _ = resolve_scope_dependency(location, dependency)
            if kind == "missing":
                issues.append(
                    f"scope {claim['scope']!r} depends on unfinished scope {dependency!r}"
                )
    return sorted(set(issues))


def assert_scope_dependencies_acyclic(
    claims: list[dict[str, object]],
    mode: str,
) -> None:
    scopes = [str(claim["scope"]) for claim in claims]
    selected = set(scopes)
    graph: dict[str, set[str]] = {scope: set() for scope in scopes}
    for claim in claims:
        scope = str(claim["scope"])
        graph[scope].update(
            dependency
            for dependency in string_list(claim, "depends_on")
            if dependency in selected
        )
    if mode == "ordered-tx":
        for prior, current in zip(scopes, scopes[1:]):
            graph[current].add(prior)
    _assert_graph_acyclic(graph, "scope dependency")


def _assert_graph_acyclic(graph: dict[str, set[str]], label: str) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            start = visiting.index(node)
            cycle = visiting[start:] + [node]
            raise ValueError(f"{label} cycle: {' -> '.join(cycle)}")
        if node in visited:
            return
        visiting.append(node)
        for dependency in sorted(graph.get(node, set())):
            if dependency in graph:
                visit(dependency)
        visiting.pop()
        visited.add(node)

    for node in sorted(graph):
        visit(node)


def assert_publish_dependencies_acyclic(
    location: Path,
    proposed: list[dict[str, object]],
) -> None:
    records: dict[str, dict[str, object]] = {}
    for _, record in active_transactions(location):
        records[str(record["transaction_id"])] = record
    for record in _planned_group_transactions(location):
        records[str(record["transaction_id"])] = record
    for record in proposed:
        records[str(record["transaction_id"])] = record
    graph = {
        transaction_id: set(string_list(record, "publish_after"))
        for transaction_id, record in records.items()
    }
    _assert_graph_acyclic(graph, "publish dependency")


def _dependency_transaction_ids(
    location: Path,
    claims: list[dict[str, object]],
    scope_ids: dict[str, str],
    mode: str,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    prior: str | None = None
    for claim in claims:
        scope = str(claim["scope"])
        dependencies: list[str] = []
        if mode == "ordered-tx" and prior is not None:
            dependencies.append(prior)
        for dependency_scope in string_list(claim, "depends_on"):
            if dependency_scope in scope_ids:
                transaction_id = scope_ids[dependency_scope]
            else:
                kind, transaction_id = resolve_scope_dependency(
                    location, dependency_scope
                )
                if kind == "released":
                    continue
                if kind == "missing" or transaction_id is None:
                    raise ValueError(
                        f"scope {scope!r} depends on unfinished scope "
                        f"{dependency_scope!r}"
                    )
            if transaction_id not in dependencies:
                dependencies.append(transaction_id)
        result[scope] = dependencies
        prior = scope_ids[scope]
    return result


def activate_transaction_group(
    root: Path,
    location: Path,
    scopes: list[str],
    mode: str,
    steward: str,
    canonical_branch: str,
    reason: str,
    request_id: str | None = None,
) -> list[dict[str, object]]:
    if mode not in TRANSACTION_MODES:
        raise ValueError(f"mode must be one of {', '.join(sorted(TRANSACTION_MODES))}")
    if len(scopes) < 2:
        raise ValueError("a contention transaction group requires at least two scopes")
    claims_with_paths = load_claims(location, scopes)
    claims = [record for _, record in claims_with_paths]
    assert_scope_dependencies_acyclic(claims, mode)
    selected_paths = sorted(
        {path for record in claims for path in record_paths(record)}
    )
    dirty_overlap = [
        path
        for path in git.status_paths(root)
        if any(paths_overlap(path, declared) for declared in selected_paths)
    ]
    if dirty_overlap:
        raise ValueError(
            "cannot promote claims after overlapping writes started: "
            + ", ".join(dirty_overlap)
        )
    selected_scopes = set(scopes)
    for _, other in active_claims(location):
        other_scope = str(other.get("scope", ""))
        if other_scope in selected_scopes:
            continue
        if request_id is not None and other.get("status") == "pending-arbitration":
            continue
        overlaps = overlapping_pairs(selected_paths, record_paths(other))
        if overlaps:
            raise ValueError(
                f"unselected active claim {other_scope!r} overlaps the transaction group: "
                + ", ".join(overlaps)
            )
    for _, other in active_transactions(location):
        overlaps = overlapping_pairs(selected_paths, record_paths(other))
        if overlaps:
            raise ValueError(
                f"active transaction {other.get('transaction_id')!r} overlaps: "
                + ", ".join(overlaps)
            )
    for _, group in active_groups(location):
        overlaps = overlapping_pairs(selected_paths, group_declared_paths(group))
        if overlaps:
            raise ValueError(
                f"active transaction group {group.get('group_id')!r} overlaps: "
                + ", ".join(overlaps)
            )

    base = git.current_head(root)
    scope_ids = {scope: make_transaction_id(scope) for scope in scopes}
    dependency_ids = _dependency_transaction_ids(
        location, claims, scope_ids, mode
    )
    records: list[dict[str, object]] = []
    for claim in claims:
        scope = str(claim["scope"])
        transaction_id = scope_ids[scope]
        record: dict[str, object] = {
            "schema": 1,
            "transaction_id": transaction_id,
            "scope": scope,
            "owner": claim["owner"],
            "task": claim["task"],
            "intent": claim_intent(claim),
            "paths": record_paths(claim),
            "semantic_writes": string_list(claim, "semantic_writes"),
            "sensitive_to": string_list(claim, "sensitive_to"),
            "validation_plan": string_list(claim, "validation"),
            "first_release": claim.get("first_release", ""),
            "source_claim_status": claim.get("status", "active"),
            "decision": mode,
            "decision_reason": reason,
            "steward": steward,
            "canonical_branch": canonical_branch,
            "base_revision": base,
            "branch": f"agent-tx/{transaction_id}",
            "checkout": str(location / "checkouts" / transaction_id),
            "publish_after": dependency_ids[scope],
            "status": "materializing",
            "created_at": now(),
        }
        if request_id is not None:
            record["request_id"] = request_id
        records.append(record)
    assert_publish_dependencies_acyclic(location, records)
    group_path, group = create_group(
        location,
        mode,
        reason,
        steward,
        canonical_branch,
        base,
        records,
        request_id=request_id,
    )
    return materialize_group(root, location, group_path, group)
