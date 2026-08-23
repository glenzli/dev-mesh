"""Bounded, action-oriented CLI projections; authority records stay unchanged."""

from __future__ import annotations

from collections.abc import Mapping


MAX_COMPACT_ITEMS = 8

_FIELDS = (
    "protocol",
    "protocol_version",
    "state_root",
    "scope",
    "kind",
    "owner",
    "run_id",
    "source_owner",
    "source_run_id",
    "target_owner",
    "target_run_id",
    "status",
    "outcome",
    "event_id",
    "message_id",
    "handoff_id",
    "contention_id",
    "decision",
    "decision_revision",
    "epoch",
    "lease_expires_at",
    "trigger_scope",
    "work_state_id",
    "disposition",
    "blocked_by_owner",
    "alternate_scope",
    "transaction_id",
    "direct_commit_id",
    "result_id",
    "projection_mode",
    "workspace_bytes_sha256",
    "workspace_file_count",
    "workspace_missing_path_count",
    "workspace_total_bytes",
    "baseline_sha256",
    "evidence_sha256",
    "baseline_changed",
    "baseline_accepted",
    "retry_required",
    "source_kind",
    "cleanup_id",
    "candidate_revision",
    "base_revision",
    "canonical_branch",
    "branch",
    "checkout",
    "archive",
    "error_kind",
    "blocker_kind",
    "blocker_count",
    "interaction_kind",
    "topic",
    "requires_ack",
    "collaboration_id",
    "phase",
    "actor_role",
    "source_workspace_id",
    "target_workspace_id",
    "target_task_id",
    "reconciled",
    "reason_code",
    "plan_digest",
    "cutover_id",
    "verified",
    "source_version",
    "target_version",
    "source_disposition",
    "policy",
    "path",
    "record_sha256",
    "total_event_count",
    "retained_event_count",
    "omitted_event_count",
    "events_truncated",
    "claim_reused",
    "requested_scope",
    "completion_kind",
    "work_result_created",
)

_COLLECTION_KEYS = {
    "activated",
    "attention",
    "completed",
    "in_flight",
    "publications",
    "aborts",
    "cleanups",
    "active_direct_commits",
    "active_transactions",
    "active_cleanups",
    "orphan_checkouts",
    "missing_checkouts",
    "unregistered_checkout_directories",
    "orphan_branches",
    "missing_branches",
    "mismatched_checkout_branches",
    "invalid_checkout_records",
    "atomic_temp_residues",
    "cleanup_attention",
}


def _record(value: Mapping[str, object]) -> dict[str, object]:
    projected = {field: value[field] for field in _FIELDS if field in value}
    coordinator = value.get("coordinator")
    if isinstance(coordinator, Mapping):
        projected["coordinator"] = _record(coordinator)
    participants = value.get("participants")
    if isinstance(participants, list):
        projected["participants"] = _collection(participants)
    responses = value.get("responses")
    if isinstance(responses, Mapping):
        projected["responses"] = _mapping_collection(responses)
    cleanup = value.get("cleanup")
    if isinstance(cleanup, Mapping):
        projected["cleanup"] = _record(cleanup)
    retention = value.get("analysis_retention")
    if isinstance(retention, Mapping):
        projected["analysis_retention"] = _record(retention)
    baseline = value.get("baseline")
    if isinstance(baseline, Mapping):
        projected["baseline"] = {
            key: baseline[key]
            for key in (
                "baseline_sha256",
                "evidence_sha256",
                "actual_path_count",
                "actual_paths_sha256",
                "actual_path_sample",
                "projection_mode",
                "workspace_bytes_sha256",
                "workspace_file_count",
                "workspace_missing_path_count",
                "workspace_total_bytes",
            )
            if key in baseline
        }
    status = value.get("status")
    if status in {"pending-arbitration", "pending-baseline"}:
        projected["write_authority"] = "none"
    elif status == "active" and value.get("scope") is not None:
        projected["write_authority"] = "granted"
    if status == "pending-baseline" and isinstance(baseline, Mapping):
        projected["required_action"] = "inspect_declared_paths_then_accept_exact_baseline"
        projected["accept_baseline_sha256"] = baseline.get("baseline_sha256")
        if value.get("baseline_changed") is True:
            projected["retry_required"] = True
    return projected


def _collection(value: list[object]) -> dict[str, object]:
    sample = [
        _record(item) if isinstance(item, Mapping) else item
        for item in value[:MAX_COMPACT_ITEMS]
    ]
    return {
        "count": len(value),
        "sample": sample,
        "truncated": len(value) > MAX_COMPACT_ITEMS,
    }


def _mapping_collection(value: Mapping[object, object]) -> dict[str, object]:
    ordered = sorted(value.items(), key=lambda item: str(item[0]))
    sample = [
        {
            "key": str(key),
            "value": _record(item) if isinstance(item, Mapping) else item,
        }
        for key, item in ordered[:MAX_COMPACT_ITEMS]
    ]
    return {
        "count": len(ordered),
        "sample": sample,
        "truncated": len(ordered) > MAX_COMPACT_ITEMS,
    }


def _next_action(command: str, value: Mapping[str, object]) -> str | None:
    status = value.get("status")
    if command == "join":
        return "inspect_scoped_status_then_claim"
    if command in {"claim", "claim-activate", "claim-resume", "claim-pause", "claim-baseline-accept"}:
        if status == "pending-arbitration":
            return "stop_overlap_writes_and_coordinate"
        if status == "pending-baseline":
            if value.get("baseline_changed") is True:
                return "review_changed_baseline_then_retry_accept"
            return "review_and_accept_inherited_baseline"
        if status == "paused":
            return "wait_for_resume_condition"
        if status == "active":
            return "edit_and_validate_declared_scope"
    if command == "direct-commit":
        return (
            "release_claim"
            if status == "completed"
            else "run_direct_commit_reconcile_with_verbose_output"
        )
    if command in {"claim-complete", "claim-finish"}:
        return "leave_when_no_owned_authority_remains"
    if command == "publish-results":
        return (
            "leave_when_no_owned_authority_remains"
            if status == "completed"
            else "run_direct_commit_reconcile_with_verbose_output"
        )
    if command == "claim-release":
        return "leave_when_no_owned_authority_remains"
    if command == "leave":
        return "done"
    if command == "contention-propose":
        return "collect_exact_revision_responses"
    if command == "contention-wait":
        return "wait_for_overlap_release_then_activate_claim"
    if command == "contention-respond":
        return "coordinator_enacts_after_all_participants_accept"
    if command in {"contention-enact", "contention-cancel"}:
        return "follow_terminal_decision"
    if command == "contention-open":
        return "coordinator_proposes_bounded_decision"
    if command == "send":
        return (
            "ensure_actual_task_delivery_then_wait_for_acknowledgement"
            if value.get("requires_ack")
            else "ensure_actual_task_delivery"
        )
    if command == "cross-project-open":
        return "include_correlation_in_target_task_message"
    if command == "cross-project-bind":
        return "perform_requested_work_then_close_relation"
    if command in {"cross-project-close", "cross-project-reconcile-close"}:
        return "done"
    if command == "ack":
        return "complete_explicit_authority_transfer_if_needed"
    if command == "work-suspend":
        return "continue_alternate_scope" if value.get("disposition") == "diverted" else "wait"
    if command == "work-resume":
        return "continue_declared_scope"
    if command == "tx-begin" and status == "active":
        return "edit_only_the_transaction_checkout"
    if command in {"tx-prepare", "tx-publish"} and status == "prepared":
        return "validate_exact_candidate"
    if command == "tx-validate" and status == "ready":
        return "request_serialized_publication"
    if status in {"conflicted", "needs-attention", "initialization-needs-attention"}:
        return "preserve_state_and_inspect_verbose_recovery_facts"
    if command.endswith("reconcile"):
        return "inspect_attention_only_if_nonzero"
    return None


def _matches(
    value: Mapping[str, object],
    *,
    owner: str | None,
    run_id: str | None,
    scopes: set[str],
) -> bool:
    if owner is not None and value.get("owner") != owner:
        return False
    if run_id is not None and value.get("run_id") != run_id:
        return False
    if scopes and value.get("scope") not in scopes:
        return False
    return True


def _select_status(
    value: Mapping[str, object],
    *,
    owner: str | None,
    run_id: str | None,
    scopes: set[str],
) -> dict[str, object]:
    runs = [item for item in value.get("runs", []) if isinstance(item, Mapping)]
    claims = [item for item in value.get("claims", []) if isinstance(item, Mapping)]
    selected_runs = [
        item
        for item in runs
        if _matches(item, owner=owner, run_id=run_id, scopes=set())
    ]
    selected_claims = [
        item
        for item in claims
        if _matches(item, owner=owner, run_id=run_id, scopes=scopes)
    ]
    if scopes:
        claim_run_ids = {str(item.get("run_id")) for item in selected_claims}
        selected_runs = [
            item for item in runs if str(item.get("run_id")) in claim_run_ids
        ]
    selected_run_ids = {str(item.get("run_id")) for item in selected_runs}
    blockers = value.get("blockers", {})
    selected_blockers = (
        {
            key: item
            for key, item in blockers.items()
            if isinstance(key, str)
            and (not (owner or run_id or scopes) or key in selected_run_ids)
        }
        if isinstance(blockers, Mapping)
        else {}
    )
    return {
        "protocol": value.get("protocol"),
        "runs": selected_runs,
        "claims": selected_claims,
        "blockers": selected_blockers,
    }


def _compact_status(value: Mapping[str, object], *, filtered: bool) -> dict[str, object]:
    runs = [item for item in value.get("runs", []) if isinstance(item, Mapping)]
    claims = [item for item in value.get("claims", []) if isinstance(item, Mapping)]
    blockers = value.get("blockers", {})
    blocker_items = [
        {"kind": "run", "run_id": run_id, "blocker_count": len(items)}
        for run_id, items in blockers.items()
        if isinstance(blockers, Mapping) and isinstance(items, list) and items
    ]
    pending = [
        {
            "kind": "claim",
            **_record(item),
            "next_action": _next_action("claim-pause" if item.get("status") == "paused" else "claim", item),
        }
        for item in claims
        if item.get("status") in {"pending-arbitration", "paused"}
    ]
    result: dict[str, object] = {
        "protocol": value.get("protocol"),
        "counts": {
            "runs": len(runs),
            "active_runs": sum(item.get("status") == "active" for item in runs),
            "claims": len(claims),
            "active_claims": sum(item.get("status") == "active" for item in claims),
            "pending_claims": sum(
                item.get("status") == "pending-arbitration" for item in claims
            ),
            "leave_blocked_runs": len(blocker_items),
        },
        "action_required": _collection(pending),
        "leave_constraints": _collection(blocker_items),
    }
    if filtered:
        result["runs"] = _collection(runs)
        result["claims"] = _collection(claims)
    else:
        result["hint"] = "filter with --owner, --run-id, or --scope; use --verbose only for exact recovery facts"
    return result


def project(
    command: str,
    value: object,
    *,
    verbose: bool,
    owner: str | None = None,
    run_id: str | None = None,
    scopes: list[str] | None = None,
) -> object:
    """Return a bounded presentation without changing producer authority or events."""

    if not isinstance(value, Mapping):
        return value
    selected: Mapping[str, object] = value
    filtered = bool(owner or run_id or scopes)
    if command == "status":
        selected = _select_status(
            value,
            owner=owner,
            run_id=run_id,
            scopes=set(scopes or []),
        )
        if verbose:
            return dict(selected)
        return _compact_status(selected, filtered=filtered)
    if verbose:
        result = dict(selected)
        if command == "contention-wait":
            result["write_authority"] = "none"
        if command == "send":
            result["next_action"] = _next_action(command, selected)
            result["dev_mesh_effect"] = "record_persisted"
            result["external_task_delivery"] = "not_performed_by_dev_mesh"
            result["target_task_woken_by_dev_mesh"] = False
        return result
    result = _record(selected)
    if command == "contention-wait":
        result["write_authority"] = "none"
    for key, item in selected.items():
        if key in _COLLECTION_KEYS and isinstance(item, list):
            result[key] = _collection(item)
    next_action = _next_action(command, selected)
    if next_action is not None:
        result["next_action"] = next_action
    if command == "send":
        result["dev_mesh_effect"] = "record_persisted"
        result["external_task_delivery"] = "not_performed_by_dev_mesh"
        result["target_task_woken_by_dev_mesh"] = False
    return result
