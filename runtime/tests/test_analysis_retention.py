from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dev_mesh_coord import analysis_retention


class AnalysisRetentionTest(unittest.TestCase):
    def test_retention_is_bounded_decontented_and_authority_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            events = state / "events"
            events.mkdir()
            for index in range(3):
                (events / f"{index}.json").write_text(
                    json.dumps(
                        {
                            "schema": 2,
                            "event_id": f"event-{index}",
                            "event": "message-sent" if index < 2 else "contention-opened",
                            "at": f"2026-08-23T00:00:0{index}Z",
                            "authority_effect": "none",
                            "owner": "agent-a",
                            "run_id": "run-a",
                            "source_owner": "agent-a",
                            "source_run_id": "run-a",
                            "target_owner": "agent-b",
                            "subject": "drop subject",
                            "body": "drop body",
                            "reason": "drop reason",
                            "paths": ["drop/path.txt"],
                            "cross_project": {
                                "protocol": "dev-mesh.cross-project-collaboration",
                                "protocol_version": "20260814.1",
                                "collaboration_id": "cross-review",
                                "phase": "opened",
                                "kind": "review",
                                "actor_role": "source",
                                "source": {
                                    "workspace_id": "a" * 24,
                                    "owner": "agent-a",
                                    "run_id": "run-a",
                                },
                                "target": {
                                    "workspace_id": "b" * 24,
                                    "task_id": "drop-task-id",
                                    "owner": "agent-b",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            (events / "invalid.json").write_text("not json", encoding="utf-8")

            with mock.patch.object(analysis_retention, "MAX_RETAINED_EVENTS", 2):
                retained = analysis_retention.build_analysis_retention(
                    state,
                    source_version="20260814.1",
                    source_event_schema=2,
                    source_state_sha256="source-digest",
                    recorded_at="2026-08-23T01:00:00Z",
                )

        self.assertEqual(retained["authority"], "none")
        self.assertEqual(retained["total_event_count"], 4)
        self.assertEqual(retained["valid_event_count"], 3)
        self.assertEqual(retained["invalid_event_count"], 1)
        self.assertEqual(retained["retained_event_count"], 2)
        self.assertEqual(retained["omitted_event_count"], 1)
        self.assertTrue(retained["events_truncated"])
        self.assertEqual(
            retained["event_counts"],
            {"contention-opened": 1, "message-sent": 2},
        )
        self.assertEqual(
            [event["event_id"] for event in retained["events"]],
            ["event-1", "event-2"],
        )
        encoded = json.dumps(retained)
        for forbidden in (
            "drop subject",
            "drop body",
            "drop reason",
            "drop/path.txt",
            "drop-task-id",
        ):
            self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()
