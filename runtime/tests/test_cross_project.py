from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from dev_mesh_coord import cross_project
from dev_mesh_coord.cli import main
from dev_mesh_coord.control_plane import initialize, resolve
from dev_mesh_coord.lifecycle import join_run

from helpers import GitWorkspaceTest, git


class CrossProjectCollaborationTest(GitWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        self.target = Path(self.temporary.name) / "target"
        self.target.mkdir()
        git(self.target, "init", "-b", "main")
        git(self.target, "config", "user.name", "Dev Mesh Test")
        git(self.target, "config", "user.email", "dev-mesh@example.invalid")
        (self.target / "app.txt").write_text("target\n", encoding="utf-8")
        git(self.target, "add", "app.txt")
        git(self.target, "commit", "-m", "base")
        initialize(self.root)
        initialize(self.target)
        join_run(self.root, run_id="source-run", owner="source-agent", task="request review")
        join_run(self.target, run_id="target-run", owner="target-agent", task="perform review")

    def _events(self, root: Path) -> list[dict[str, object]]:
        plane = resolve(root)
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((plane.state_root / "events").glob("*-message-sent.json"))
        ]

    def test_open_bind_close_are_bounded_and_idempotent(self) -> None:
        collaboration_id = "echo-infer-review-20260813"
        source_workspace_id = cross_project.workspace_id(self.root)
        target_workspace_id = cross_project.workspace_id(self.target)
        opened = cross_project.open_collaboration(
            self.root,
            collaboration_id=collaboration_id,
            source_owner="source-agent",
            source_run_id="source-run",
            target_task_id="task-infer-3",
            target_workspace_id=target_workspace_id,
            target_owner="target-agent",
            kind="review",
        )
        bound = cross_project.bind_collaboration(
            self.target,
            collaboration_id=collaboration_id,
            source_workspace_id=source_workspace_id,
            source_owner="source-agent",
            source_run_id="source-run",
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="task-infer-3",
            kind="review",
        )
        closed = cross_project.close_collaboration(
            self.target,
            collaboration_id=collaboration_id,
            actor_role="target",
            owner="target-agent",
            run_id="target-run",
            source_workspace_id=source_workspace_id,
            source_owner="source-agent",
            source_run_id="source-run",
            target_workspace_id=target_workspace_id,
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="task-infer-3",
            kind="review",
            outcome="completed",
        )
        self.assertEqual(opened["phase"], "opened")
        self.assertEqual(bound["phase"], "bound")
        self.assertEqual(closed["outcome"], "completed")

        again = cross_project.bind_collaboration(
            self.target,
            collaboration_id=collaboration_id,
            source_workspace_id=source_workspace_id,
            source_owner="source-agent",
            source_run_id="source-run",
            target_owner="target-agent",
            target_run_id="target-run",
            target_task_id="task-infer-3",
            kind="review",
        )
        self.assertEqual(again["message_id"], bound["message_id"])
        self.assertEqual(len(self._events(self.root)), 1)
        self.assertEqual(len(self._events(self.target)), 2)
        event_paths = [
            *resolve(self.root).state_root.joinpath("events").glob("*-message-sent.json"),
            *resolve(self.target).state_root.joinpath("events").glob("*-message-sent.json"),
        ]
        self.assertEqual(len(event_paths), 3)
        self.assertLess(max(path.stat().st_size for path in event_paths), 4096)
        for event in [*self._events(self.root), *self._events(self.target)]:
            self.assertEqual(event["event"], "message-sent")
            self.assertEqual(event["protocol_version"], "20260812.1")
            self.assertEqual(event["authority_effect"], "none")
            extension = event["cross_project"]
            self.assertEqual(extension["protocol_version"], "20260813.1")
            self.assertEqual(extension["collaboration_id"], collaboration_id)
            self.assertNotIn("body", event)
            self.assertNotIn("prompt", event)

    def test_retry_repairs_event_gap_and_rejects_changed_facts(self) -> None:
        arguments = {
            "collaboration_id": "stable-cross-project-id",
            "source_owner": "source-agent",
            "source_run_id": "source-run",
            "target_task_id": "target-task",
            "kind": "dependency",
        }
        with mock.patch.object(cross_project, "write_event", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                cross_project.open_collaboration(self.root, **arguments)
        self.assertEqual(self._events(self.root), [])
        repaired = cross_project.open_collaboration(self.root, **arguments)
        self.assertEqual(len(self._events(self.root)), 1)
        snapshot = json.loads(
            resolve(self.root)
            .state_root.joinpath("messages", f"{repaired['message_id']}.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(self._events(self.root)[0]["event_id"], snapshot["event"]["event_id"])
        with self.assertRaisesRegex(ValueError, "differs from the original"):
            cross_project.open_collaboration(
                self.root,
                **{**arguments, "target_owner": "target-agent"},
            )

    def test_close_requires_the_actor_to_match_its_workspace_and_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "actor role does not match"):
            cross_project.close_collaboration(
                self.target,
                collaboration_id="wrong-actor",
                actor_role="source",
                owner="target-agent",
                run_id="target-run",
                source_workspace_id=cross_project.workspace_id(self.root),
                source_owner="source-agent",
                source_run_id="source-run",
                target_workspace_id=cross_project.workspace_id(self.target),
                target_owner="target-agent",
                target_run_id="target-run",
                target_task_id="target-task",
                kind="request",
                outcome="completed",
            )

    def test_cli_returns_the_correlation_needed_by_the_target_task(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "--root",
                    str(self.root),
                    "cross-project-open",
                    "--collaboration-id",
                    "cli-cross-project",
                    "--source-owner",
                    "source-agent",
                    "--source-run-id",
                    "source-run",
                    "--target-task-id",
                    "target-task",
                    "--kind",
                    "request",
                ]
            )
        self.assertEqual(result, 0)
        projected = json.loads(output.getvalue())
        self.assertEqual(projected["collaboration_id"], "cli-cross-project")
        self.assertEqual(projected["phase"], "opened")
        self.assertEqual(projected["source_workspace_id"], cross_project.workspace_id(self.root))
        self.assertEqual(
            projected["next_action"], "include_correlation_in_target_task_message"
        )
