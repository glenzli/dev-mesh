"""Deterministic semantic compatibility recommendations for claims."""

from __future__ import annotations

from .state import (
    EXCLUSIVE_INTENTS,
    INTENTS,
    MERGEABLE_INTENTS,
    overlapping_pairs,
    paths_overlap,
    string_list,
)


def record_paths(record: dict[str, object]) -> list[str]:
    return string_list(record, "paths")


def record_resources(record: dict[str, object], key: str) -> set[str]:
    return set(string_list(record, key))


def claim_intent(record: dict[str, object]) -> str:
    value = record.get("intent", "local-edit")
    if not isinstance(value, str) or value not in INTENTS:
        raise ValueError(f"claim has invalid intent: {value!r}")
    return value


def relevant_relationships(records: list[dict[str, object]]) -> dict[str, object]:
    physical: list[str] = []
    same_semantic: list[str] = []
    dependency_edges: list[str] = []
    unknown_semantic_overlap = False
    for left_index, left in enumerate(records):
        for right in records[left_index + 1 :]:
            left_scope = str(left.get("scope", "unknown"))
            right_scope = str(right.get("scope", "unknown"))
            path_pairs = overlapping_pairs(record_paths(left), record_paths(right))
            physical.extend(
                f"{left_scope} ↔ {right_scope}: {pair}" for pair in path_pairs
            )
            left_writes = record_resources(left, "semantic_writes")
            right_writes = record_resources(right, "semantic_writes")
            shared_writes = sorted(left_writes & right_writes)
            same_semantic.extend(
                f"{left_scope} ↔ {right_scope}: {value}" for value in shared_writes
            )
            left_sensitive = record_resources(left, "sensitive_to")
            right_sensitive = record_resources(right, "sensitive_to")
            dependencies = sorted(
                (left_writes & right_sensitive) | (right_writes & left_sensitive)
            )
            dependency_edges.extend(
                f"{left_scope} ↔ {right_scope}: {value}" for value in dependencies
            )
            if path_pairs and (not left_writes or not right_writes):
                unknown_semantic_overlap = True
    return {
        "physical": sorted(set(physical)),
        "same_semantic": sorted(set(same_semantic)),
        "dependencies": sorted(set(dependency_edges)),
        "unknown_semantic_overlap": unknown_semantic_overlap,
    }


def recommend_decision(
    records: list[dict[str, object]],
    dirty_paths: list[str],
) -> dict[str, object]:
    relationships = relevant_relationships(records)
    union_paths = sorted({path for record in records for path in record_paths(record)})
    dirty_overlap = sorted(
        {
            f"{dirty} ↔ {declared}"
            for dirty in dirty_paths
            for declared in union_paths
            if paths_overlap(dirty, declared)
        }
    )
    intents = [claim_intent(record) for record in records]
    if dirty_overlap:
        decision = "wait"
        reason = "overlapping direct work is already dirty"
    elif any(intent in EXCLUSIVE_INTENTS for intent in intents) and (
        relationships["physical"]
        or relationships["same_semantic"]
        or relationships["dependencies"]
    ):
        decision = "exclusive"
        reason = "an exclusive semantic intent overlaps another claim"
    elif relationships["same_semantic"]:
        decision = "wait-or-handoff"
        reason = "claims write the same semantic resource"
    elif not relationships["physical"] and not relationships["dependencies"]:
        decision = "direct"
        reason = "claims have no relevant physical or semantic overlap"
    elif (
        relationships["physical"]
        and not relationships["dependencies"]
        and not relationships["unknown_semantic_overlap"]
        and all(intent in MERGEABLE_INTENTS for intent in intents)
    ):
        decision = "parallel-tx"
        reason = "mergeable claims overlap physically but declare disjoint semantic writes"
    else:
        decision = "arbitration-required"
        reason = "the relationship or cost tradeoff is not deterministic"
    return {
        "recommendation": decision,
        "reason": reason,
        "intents": intents,
        "dirty_overlap": dirty_overlap,
        "relationships": relationships,
    }
