from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_SCRIPTS = PROJECT_ROOT / "skills" / "observe-dev-mesh" / "scripts"
sys.path.insert(0, str(OBSERVER_SCRIPTS))

from dev_mesh_observer.analytics import build_coordination_analytics  # noqa: E402


class ObserverAnalyticsTest(unittest.TestCase):
    @staticmethod
    def event(
        at: str,
        event: str,
        *,
        owner: str | None = None,
        run_id: str | None = None,
        transaction_id: str | None = None,
        **payload: object,
    ) -> dict[str, object]:
        body = {"at": at, "event": event, **payload}
        if owner is not None:
            body["owner"] = owner
        if run_id is not None:
            body["run_id"] = run_id
        if transaction_id is not None:
            body["transaction_id"] = transaction_id
        return {
            "workspace_id": "workspace-1",
            "event_at": at,
            "event_type": event,
            "run_id": run_id,
            "handoff_id": body.get("handoff_id"),
            "transaction_id": transaction_id,
            "owner": owner,
            "payload_json": json.dumps(body),
        }

    def test_derives_conflicts_transactions_and_solo_protocol_runs(self) -> None:
        rows = [
            self.event("2026-08-11T00:00:00Z", "agent-joined", owner="root", run_id="run-root"),
            self.event(
                "2026-08-11T00:01:00Z",
                "agent-joined",
                owner="child",
                run_id="run-child",
                parent_agent_id="root",
            ),
            self.event("2026-08-11T00:02:00Z", "agent-left", owner="child", run_id="run-child"),
            self.event("2026-08-11T00:03:00Z", "agent-left", owner="root", run_id="run-root"),
            self.event("2026-08-11T01:00:00Z", "agent-joined", owner="solo", run_id="run-solo"),
            self.event(
                "2026-08-11T01:01:00Z",
                "claim-created",
                owner="solo",
                scope="solo-scope",
                paths=["src/solo.py"],
            ),
            self.event(
                "2026-08-11T01:02:00Z",
                "claim-released",
                owner="solo",
                scope="solo-scope",
                paths=["src/solo.py"],
            ),
            self.event("2026-08-11T01:03:00Z", "agent-left", owner="solo", run_id="run-solo"),
            self.event("2026-08-11T01:30:00Z", "agent-joined", owner="empty", run_id="run-empty"),
            self.event("2026-08-11T01:31:00Z", "agent-left", owner="empty", run_id="run-empty"),
            self.event("2026-08-11T02:00:00Z", "agent-joined", owner="open", run_id="run-open"),
            self.event(
                "2026-08-11T04:00:00Z",
                "contention-opened",
                owner="root",
                paths=["src/shared.py", "src/other.py"],
                owners=["root", "child"],
            ),
            self.event(
                "2026-08-11T04:01:00Z",
                "queue-blocked",
                paths=["src/shared.py"],
            ),
            self.event(
                "2026-08-11T04:02:00Z",
                "transaction-activated",
                transaction_id="tx-1",
            ),
            self.event(
                "2026-08-11T04:03:00Z",
                "transaction-prepared",
                transaction_id="tx-1",
                actual_paths=["src/shared.py"],
            ),
            self.event(
                "2026-08-11T04:04:00Z",
                "refresh-conflicted",
                transaction_id="tx-1",
                conflicts=["src/shared.py"],
            ),
            self.event(
                "2026-08-11T04:05:00Z",
                "refresh-completed",
                transaction_id="tx-1",
            ),
            self.event(
                "2026-08-11T04:06:00Z",
                "transaction-validated",
                transaction_id="tx-1",
            ),
            self.event(
                "2026-08-11T04:07:00Z",
                "publish-completed",
                transaction_id="tx-1",
            ),
            self.event(
                "2026-08-11T04:08:00Z",
                "cleanup-needs-attention",
                transaction_id="tx-1",
            ),
        ]
        report = build_coordination_analytics(
            rows,
            since=datetime(2026, 8, 10, tzinfo=UTC),
            workspace_names={"workspace-1": "/workspaces/example"},
            limit=10,
            current=datetime(2026, 8, 11, 5, tzinfo=UTC),
        )

        conflicts = report["conflicts"]
        self.assertEqual(conflicts["summary"]["signals"], 4)
        self.assertEqual(conflicts["summary"]["refresh_conflicts"], 1)
        self.assertEqual(conflicts["resource_hotspots"][0]["resource"], "src/shared.py")
        self.assertEqual(conflicts["resource_hotspots"][0]["signals"], 3)

        transactions = report["transactions"]
        self.assertEqual(transactions["summary"]["observed"], 1)
        self.assertEqual(transactions["summary"]["published"], 1)
        self.assertEqual(transactions["summary"]["conflicted"], 1)
        self.assertEqual(transactions["summary"]["attention"], 1)
        self.assertEqual(transactions["recent"][0]["status"], "published")

        protocol = report["protocol_use"]
        self.assertEqual(protocol["summary"]["runs_analyzed"], 5)
        self.assertEqual(protocol["summary"]["collaborative"], 2)
        self.assertEqual(protocol["summary"]["solo_protocol"], 1)
        self.assertEqual(protocol["summary"]["lifecycle_only"], 1)
        self.assertEqual(protocol["summary"]["open_unclassified"], 1)
        self.assertEqual(protocol["solo_runs"][0]["run_id"], "run-solo")


if __name__ == "__main__":
    unittest.main()
