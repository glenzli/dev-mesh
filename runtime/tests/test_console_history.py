from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


class ConsoleHistoryTest(unittest.TestCase):
    def test_history_grouping_is_chronological_scoped_and_preserves_run_identity(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is unavailable')
        module = (Path(__file__).parents[1] / 'dev_mesh_console/web/activity_history.js').as_uri()
        script = '''
import assert from 'node:assert/strict';
import {collaborationHistory, activeWorkGroups} from MODULE;
const opened = {event_id: 'a', event: 'contention-opened', workspace_id: 'one', contention_id: 'same-id', at: '2026-09-20T08:00:00Z',
 details: {contention_participants: [{owner: 'same-owner', run_id: 'run-a', scope: 'a'}, {owner: 'same-owner', run_id: 'run-b', scope: 'b'}]}};
const ended = {...opened, event_id: 'b', event: 'contention-completed', at: '2026-09-20T08:00:01Z'};
const other = {...opened, event_id: 'c', workspace_id: 'two'};
const message = {event_id: 'd', event: 'message-sent', workspace_id: 'one', at: '2026-09-20T08:01:00Z', details: {message_id: 'message', source_owner: 'sender', source_run_id: 'source', target_owner: 'alias'}};
const groups = collaborationHistory([ended, other, opened, message, opened]);
assert.equal(groups.length, 3);
const group = groups.find(g => g.workspaceId === 'one' && g.kind === 'contention');
assert.deepEqual(group.steps.map(e => e.event_id), ['a', 'b']);
assert.deepEqual(group.participants.map(p => p.run_id), ['run-a', 'run-b']);
assert.equal(groups[0].kind, 'message');
assert.equal(groups[0].participants[1].run_id, undefined);
assert.equal(collaborationHistory([other, opened], 'one').length, 1);
assert.equal(collaborationHistory([opened])[0].latest.event, 'contention-opened');
const active = activeWorkGroups([
 {kind: 'run', workspace_id: 'one', owner: 'owner', run_id: 'run'},
 {kind: 'claim', workspace_id: 'one', owner: 'owner', run_id: 'run', object_id: 'scope', status: 'active'},
 {kind: 'run', workspace_id: 'two', owner: 'owner', run_id: 'run'},
 {kind: 'claim', workspace_id: 'one', owner: 'owner', run_id: 'waiting', object_id: 'pending', status: 'pending-arbitration'},
]);
assert.equal(active.length, 3);
assert.equal(active[0].runId, 'waiting');
assert.equal(active.find(g => g.workspaceId === 'one' && g.runId === 'run').objects.length, 1);
assert.equal(active.find(g => g.workspaceId === 'two').objects.length, 0);
'''.replace('MODULE', json.dumps(module))
        subprocess.run([node, '--input-type=module', '--eval', script], check=True, capture_output=True, text=True)
