"""Semantic arbitration and Git microtransaction lifecycle."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from . import git_backend as git
from .arbitration import claim_intent, recommend_decision, record_paths
from .recovery import (
    active_groups,
    crash_if_testing,
    create_group,
    group_declared_paths,
    mark_group_member_terminal,
    materialize_group,
    reconcile_groups,
    require_transaction_group_active,
)
from .state import (
    TRANSACTION_MODES,
    active_claims,
    active_transactions,
    archive_transaction,
    claim_path,
    coordination_guard,
    emit_event,
    initialize,
    now,
    overlapping_pairs,
    paths_overlap,
    read_json,
    read_transaction,
    replace_json,
    require_text,
    string_list,
    transaction_is_committed,
    validate_slug,
    write_json_exclusive,
)


def coordinator_path(location: Path) -> Path:
    return location / "steward.json"


def require_steward(location: Path, steward: str) -> str:
    steward = validate_slug(steward, "steward")
    path = coordinator_path(location)
    if not path.exists():
        raise ValueError("transaction coordinator is not initialized; run tx init")
    record = read_json(path)
    if record.get("steward") != steward:
        raise ValueError(
            f"transaction steward is {record.get('steward')!r}, not {steward!r}"
        )
    return steward


def require_canonical_branch(location: Path, root: Path) -> str:
    record = read_json(coordinator_path(location))
    expected = record.get("canonical_branch")
    if not isinstance(expected, str) or not expected:
        raise ValueError("coordinator canonical branch is malformed")
    current = git.current_branch(root)
    if current != expected:
        raise ValueError(
            f"canonical workspace is on branch {current!r}, expected {expected!r}"
        )
    return expected


def load_claims(location: Path, scopes: list[str]) -> list[tuple[Path, dict[str, object]]]:
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


def transaction_checkout(record: dict[str, object]) -> Path:
    value = record.get("checkout")
    if not isinstance(value, str) or not value:
        raise ValueError("transaction checkout is malformed")
    return Path(value)


def assert_owner(record: dict[str, object], owner: str) -> str:
    owner = validate_slug(owner, "owner")
    if record.get("owner") != owner:
        raise ValueError(f"transaction belongs to {record.get('owner')!r}, not {owner!r}")
    return owner


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_init(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    steward = validate_slug(arguments.steward, "steward")
    path = coordinator_path(location)
    with coordination_guard(location, "tx-init"):
        if path.exists():
            existing = read_json(path)
            if existing.get("steward") != steward:
                raise ValueError(
                    f"coordinator already belongs to {existing.get('steward')!r}"
                )
        else:
            write_json_exclusive(
                path,
                {
                    "schema": 1,
                    "steward": steward,
                    "canonical_branch": git.current_branch(arguments.root),
                    "created_at": now(),
                },
            )
            emit_event(location, "coordinator-initialized", None, {"steward": steward})
    print_json({"state": str(location), "steward": steward})
    return 0


def command_inspect(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    claims = load_claims(location, arguments.scopes)
    result = recommend_decision(
        [record for _, record in claims],
        git.status_paths(arguments.root),
    )
    result["scopes"] = arguments.scopes
    print_json(result)
    return 0


def command_begin(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    git.ensure_local_exclude(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    canonical_branch = require_canonical_branch(location, arguments.root)
    mode = arguments.mode
    if mode not in TRANSACTION_MODES:
        raise ValueError(f"mode must be one of {', '.join(sorted(TRANSACTION_MODES))}")
    reason = require_text(arguments.reason, "arbitration reason")
    if len(arguments.scopes) < 2:
        raise ValueError("a contention transaction group requires at least two scopes")

    with coordination_guard(location, "tx-begin"):
        claims = load_claims(location, arguments.scopes)
        selected_paths = sorted(
            {path for _, record in claims for path in record_paths(record)}
        )
        dirty_overlap = [
            path
            for path in git.status_paths(arguments.root)
            if any(paths_overlap(path, declared) for declared in selected_paths)
        ]
        if dirty_overlap:
            raise ValueError(
                "cannot promote claims after overlapping writes started: "
                + ", ".join(dirty_overlap)
            )
        selected_scopes = set(arguments.scopes)
        for _, other in active_claims(location):
            other_scope = str(other.get("scope", ""))
            if other_scope in selected_scopes:
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

        base = git.current_head(arguments.root)
        records: list[dict[str, object]] = []
        prior_transaction: str | None = None
        for _, claim in claims:
            scope = str(claim["scope"])
            transaction_id = make_transaction_id(scope)
            checkout = location / "checkouts" / transaction_id
            branch = f"agent-tx/{transaction_id}"
            publish_after = (
                [prior_transaction]
                if mode == "ordered-tx" and prior_transaction is not None
                else []
            )
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
                "decision": mode,
                "decision_reason": reason,
                "steward": steward,
                "canonical_branch": canonical_branch,
                "base_revision": base,
                "branch": branch,
                "checkout": str(checkout),
                "publish_after": publish_after,
                "status": "materializing",
                "created_at": now(),
            }
            records.append(record)
            prior_transaction = transaction_id

        group_path, group = create_group(
            location,
            mode,
            reason,
            steward,
            canonical_branch,
            base,
            records,
        )
        print_json(materialize_group(arguments.root, location, group_path, group))
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    groups = [record for _, record in active_groups(location)]
    records = [record for _, record in active_transactions(location)]
    if arguments.json:
        print_json({"groups": groups, "transactions": records})
        return 0
    if not groups and not records:
        print("no active transaction groups or transactions")
        return 0
    for group in groups:
        print(
            f"group {group.get('group_id')}: status={group.get('status')} "
            f"claims_promoted={group.get('claims_promoted')} "
            f"members={len(group.get('members', []))}"
        )
    for record in records:
        print(
            f"{record.get('transaction_id')}: status={record.get('status')} "
            f"group={record.get('group_id', 'legacy')} owner={record.get('owner')} "
            f"scope={record.get('scope')} "
            f"base={record.get('base_revision')} paths={record.get('paths')}"
        )
    return 0


def command_prepare(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    summary = require_text(arguments.summary, "summary")
    with coordination_guard(location, "tx-prepare"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") not in {"active", "conflicted", "prepared", "stale"}:
            raise ValueError(f"transaction cannot be prepared from {record.get('status')!r}")
        checkout = transaction_checkout(record)
        operations = git.operation_in_progress(checkout)
        if operations:
            raise ValueError(
                "finish or abort the transaction Git operation first: " + ", ".join(operations)
            )
        declared = record_paths(record)
        dirty = git.status_paths(checkout)
        out_of_scope = git.paths_within_scope(dirty, declared)
        if out_of_scope:
            raise ValueError("transaction diff exceeds its claim: " + ", ".join(out_of_scope))

        base = str(record["base_revision"])
        root_head = git.current_head(arguments.root)
        checkout_head = git.current_head(checkout)
        if record.get("status") in {"conflicted", "stale"} and git.is_ancestor(
            checkout, root_head, checkout_head
        ):
            base = root_head
        ahead = git.commits_ahead(checkout, base)
        if dirty:
            if ahead > 1:
                raise ValueError("transaction has multiple commits plus dirty changes; squash first")
            candidate = git.stage_and_commit(
                checkout,
                declared,
                f"[tx:{record['scope']}] {summary}",
                amend=ahead == 1,
            )
        else:
            if ahead != 1:
                raise ValueError(
                    f"prepared transaction must be exactly one commit ahead of its base; found {ahead}"
                )
            candidate = checkout_head
        actual = git.diff_paths(checkout, base, candidate)
        out_of_scope = git.paths_within_scope(actual, declared)
        if out_of_scope:
            raise ValueError("candidate exceeds its claim: " + ", ".join(out_of_scope))
        if not actual:
            raise ValueError("candidate has no transaction changes")
        record["base_revision"] = base
        record["candidate"] = candidate
        record["actual_paths"] = actual
        record["summary"] = summary
        record["status"] = "prepared"
        record["prepared_at"] = now()
        record.pop("validation_evidence", None)
        record.pop("conflicts", None)
        replace_json(path, record)
        emit_event(
            location,
            "transaction-prepared",
            str(record["transaction_id"]),
            {"candidate": candidate, "actual_paths": actual},
        )
        print_json(record)
    return 0


def command_validate(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    evidence = require_text(arguments.evidence, "validation evidence")
    with coordination_guard(location, "tx-validate"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") != "prepared":
            raise ValueError("only a prepared transaction can be validated")
        checkout = transaction_checkout(record)
        if git.status_paths(checkout):
            raise ValueError("transaction checkout changed after prepare")
        candidate = str(record["candidate"])
        if git.current_head(checkout) != candidate:
            raise ValueError("transaction HEAD changed after prepare")
        record["validation_evidence"] = {
            "candidate": candidate,
            "base_revision": record["base_revision"],
            "plan": record.get("validation_plan", []),
            "evidence": evidence,
            "validated_at": now(),
        }
        record["status"] = "ready"
        replace_json(path, record)
        emit_event(
            location,
            "transaction-validated",
            str(record["transaction_id"]),
            {"candidate": candidate},
        )
        print_json(record)
    return 0


def check_publish_dependencies(location: Path, record: dict[str, object]) -> None:
    for dependency in string_list(record, "publish_after"):
        if not transaction_is_committed(location, dependency):
            raise ValueError(f"publish dependency is not committed: {dependency}")


def archive_committed_and_cleanup(
    root: Path,
    location: Path,
    path: Path,
    record: dict[str, object],
) -> tuple[Path, dict[str, object]]:
    mark_group_member_terminal(location, record, "committed")
    archive = archive_transaction(location, path, record)
    try:
        git.cleanup_published(
            root,
            transaction_checkout(record),
            str(record["branch"]),
        )
    except (OSError, ValueError, git.GitError) as error:
        record["cleanup_pending"] = True
        record["cleanup_error"] = str(error)
        replace_json(archive, record)
        emit_event(
            location,
            "cleanup-pending",
            str(record["transaction_id"]),
            {"error": str(error)},
        )
    return archive, record


def command_publish(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    steward = require_steward(location, arguments.steward)
    require_canonical_branch(location, arguments.root)
    with coordination_guard(location, "tx-publish"):
        path, record = read_transaction(location, arguments.transaction)
        if record.get("steward") != steward:
            raise ValueError("transaction belongs to a different release steward")
        require_transaction_group_active(location, record)
        if record.get("status") != "ready":
            raise ValueError("only a ready transaction can be published")
        check_publish_dependencies(location, record)
        checkout = transaction_checkout(record)
        candidate = str(record["candidate"])
        base = str(record["base_revision"])
        if git.status_paths(checkout):
            raise ValueError("ready transaction checkout is dirty")
        if git.operation_in_progress(checkout):
            raise ValueError("transaction checkout has an active Git operation")
        if git.current_head(checkout) != candidate:
            raise ValueError("transaction candidate no longer matches its branch HEAD")

        root_head = git.current_head(arguments.root)
        if root_head != base:
            record["status"] = "refreshing"
            replace_json(path, record)
            emit_event(
                location,
                "refresh-started",
                str(record["transaction_id"]),
                {"old_base": base, "new_base": root_head},
            )
            succeeded, output = git.rebase_onto(checkout, root_head)
            if not succeeded:
                record["status"] = "conflicted"
                record["conflicts"] = git.conflicted_paths(checkout)
                record["refresh_output"] = output
                replace_json(path, record)
                emit_event(
                    location,
                    "refresh-conflicted",
                    str(record["transaction_id"]),
                    {"conflicts": record["conflicts"]},
                )
                print_json(record)
                return 2
            refreshed_candidate = git.current_head(checkout)
            actual = git.diff_paths(checkout, root_head, refreshed_candidate)
            out_of_scope = git.paths_within_scope(actual, record_paths(record))
            if out_of_scope:
                raise ValueError(
                    "refreshed candidate exceeds its claim: " + ", ".join(out_of_scope)
                )
            record["base_revision"] = root_head
            record["candidate"] = refreshed_candidate
            record["actual_paths"] = actual
            record["status"] = "prepared"
            record["refreshed_at"] = now()
            record["refreshed_from"] = candidate
            record.pop("validation_evidence", None)
            replace_json(path, record)
            emit_event(
                location,
                "refresh-completed",
                str(record["transaction_id"]),
                {"candidate": refreshed_candidate},
            )
            print_json(record)
            return 2

        validation = record.get("validation_evidence")
        if not isinstance(validation, dict):
            raise ValueError("transaction has no validation evidence")
        if validation.get("candidate") != candidate or validation.get("base_revision") != base:
            raise ValueError("validation evidence is not bound to the current candidate and base")
        if git.commits_ahead(checkout, base, candidate) != 1:
            raise ValueError("candidate must be exactly one commit ahead of canonical HEAD")
        if not git.is_ancestor(checkout, base, candidate):
            raise ValueError("candidate cannot fast-forward canonical HEAD")
        root_operations = git.operation_in_progress(arguments.root)
        if root_operations:
            raise ValueError("canonical workspace has an active Git operation: " + ", ".join(root_operations))
        if not git.index_is_empty(arguments.root):
            raise ValueError("canonical Git index must be empty before transaction publish")

        actual = string_list(record, "actual_paths")
        dirty_overlap = [
            dirty
            for dirty in git.status_paths(arguments.root)
            if any(paths_overlap(dirty, changed) for changed in actual)
        ]
        if dirty_overlap:
            raise ValueError(
                "canonical workspace has overlapping dirty paths: " + ", ".join(dirty_overlap)
            )
        for _, claim in active_claims(location):
            if claim.get("status", "active") != "active":
                continue
            overlaps = overlapping_pairs(actual, record_paths(claim))
            if overlaps:
                raise ValueError(
                    f"active direct claim {claim.get('scope')!r} overlaps publish: "
                    + ", ".join(overlaps)
                )

        record["status"] = "publishing"
        record["expected_head"] = root_head
        record["publishing_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "publish-started",
            str(record["transaction_id"]),
            {"expected_head": root_head, "candidate": candidate},
        )
        crash_if_testing("publish-recorded")
        git.fast_forward(arguments.root, str(record["branch"]))
        crash_if_testing("publish-fast-forwarded")
        if git.current_head(arguments.root) != candidate:
            raise RuntimeError("Git reported success but canonical HEAD is not the candidate")
        record["status"] = "committed"
        record["committed_revision"] = candidate
        record["committed_at"] = now()
        replace_json(path, record)
        emit_event(
            location,
            "publish-completed",
            str(record["transaction_id"]),
            {"candidate": candidate},
        )
        crash_if_testing("publish-committed")
        archive, record = archive_committed_and_cleanup(
            arguments.root, location, path, record
        )
        print_json({"archive": str(archive), "transaction": record})
    return 0


def command_handoff(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    checkpoint = require_text(arguments.checkpoint, "handoff checkpoint")
    next_owner = validate_slug(arguments.next_owner, "next owner")
    with coordination_guard(location, "tx-handoff"):
        path, record = read_transaction(location, arguments.transaction)
        current_owner = assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") in {"publishing", "committed"}:
            raise ValueError("publishing or committed transactions cannot be handed off")
        record["handoff"] = {
            "from": current_owner,
            "to": next_owner,
            "checkpoint": checkpoint,
            "at": now(),
        }
        record["owner"] = next_owner
        record["status_before_pause"] = record.get("status")
        record["status"] = "paused"
        replace_json(path, record)
        emit_event(
            location,
            "transaction-handed-off",
            str(record["transaction_id"]),
            {"from": current_owner, "to": next_owner},
        )
        print_json(record)
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "tx-resume"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") != "paused":
            raise ValueError("only a paused transaction can be resumed")
        prior = record.pop("status_before_pause", "active")
        if prior not in {"active", "conflicted", "prepared", "ready", "stale"}:
            prior = "active"
        record["status"] = prior
        record["resumed_at"] = now()
        replace_json(path, record)
        emit_event(location, "transaction-resumed", str(record["transaction_id"]))
        print_json(record)
    return 0


def command_abort(arguments: argparse.Namespace) -> int:
    if not arguments.discard:
        raise ValueError("abort requires --discard to explicitly authorize data deletion")
    location = initialize(arguments.root, arguments.state_dir)
    with coordination_guard(location, "tx-abort"):
        path, record = read_transaction(location, arguments.transaction)
        assert_owner(record, arguments.owner)
        require_transaction_group_active(location, record)
        if record.get("status") in {"publishing", "committed"}:
            raise ValueError("publishing or committed transactions cannot be aborted")
        reason = require_text(arguments.reason, "abort reason")
        record["status"] = "aborted"
        record["abort_reason"] = reason
        record["aborted_at"] = now()
        mark_group_member_terminal(location, record, "aborted")
        archive = archive_transaction(location, path, record)
        try:
            git.discard_transaction(
                arguments.root,
                transaction_checkout(record),
                str(record["branch"]),
            )
        except (OSError, ValueError, git.GitError) as error:
            record["cleanup_pending"] = True
            record["cleanup_error"] = str(error)
            replace_json(archive, record)
        emit_event(
            location,
            "transaction-aborted",
            str(record["transaction_id"]),
            {"reason": reason},
        )
        print_json({"archive": str(archive), "transaction": record})
    return 0


def command_reconcile(arguments: argparse.Namespace) -> int:
    git.ensure_repository(arguments.root)
    location = initialize(arguments.root, arguments.state_dir)
    require_steward(location, arguments.steward)
    require_canonical_branch(location, arguments.root)
    with coordination_guard(location, "tx-reconcile"):
        updates = reconcile_groups(arguments.root, location)
        for path, record in active_transactions(location):
            if record.get("status") == "committed":
                archive, record = archive_committed_and_cleanup(
                    arguments.root, location, path, record
                )
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "completed",
                        "archive": str(archive),
                    }
                )
                continue
            if record.get("status") != "publishing":
                continue
            head = git.current_head(arguments.root)
            candidate = str(record.get("candidate", ""))
            expected = str(record.get("expected_head", ""))
            if head == candidate:
                record["status"] = "committed"
                record["committed_revision"] = candidate
                record["committed_at"] = now()
                replace_json(path, record)
                archive, record = archive_committed_and_cleanup(
                    arguments.root, location, path, record
                )
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "completed",
                        "archive": str(archive),
                    }
                )
            elif head == expected:
                record["status"] = "ready"
                record.pop("expected_head", None)
                replace_json(path, record)
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "retryable",
                        "transaction": record["transaction_id"],
                    }
                )
            else:
                record["status"] = "stale"
                record.pop("validation_evidence", None)
                replace_json(path, record)
                updates.append(
                    {
                        "kind": "transaction",
                        "action": "stale",
                        "transaction": record["transaction_id"],
                    }
                )
    print_json(updates)
    return 0


def command_hotspots(arguments: argparse.Namespace) -> int:
    location = initialize(arguments.root, arguments.state_dir)
    event_counts: Counter[str] = Counter()
    transaction_counts: Counter[str] = Counter()
    for path in sorted((location / "events").glob("*.json")):
        record = read_json(path)
        event_counts[str(record.get("event", "unknown"))] += 1
        transaction_id = record.get("transaction_id")
        if isinstance(transaction_id, str):
            transaction_counts[transaction_id] += 1
    print_json(
        {
            "events": dict(event_counts.most_common()),
            "transactions_by_activity": dict(transaction_counts.most_common()),
        }
    )
    return 0
