from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from dev_mesh_console.directories import DIRECTORY_LIMIT, browse_directories
from dev_mesh_console.registry import RootRegistry
from dev_mesh_console.server import ConsoleServer
from dev_mesh_console.state import ConsoleState
from helpers import GitWorkspaceTest


class ConsoleDirectoriesTest(GitWorkspaceTest):
    def test_picker_lists_only_immediate_directories_and_canonical_breadcrumbs(self):
        folder = self.root / '目录 with spaces'
        folder.mkdir()
        (folder / 'Alpha').mkdir()
        (folder / 'beta').mkdir()
        (folder / '.hidden').mkdir()
        (folder / 'file.txt').write_text('private bytes')
        (folder / 'alias').symlink_to(folder / 'Alpha', target_is_directory=True)
        before = sorted(str(path) for path in folder.iterdir())
        result = browse_directories(str(folder))
        self.assertEqual([item['name'] for item in result['directories']], ['Alpha', 'beta'])
        self.assertEqual(result['path'], str(folder.resolve()))
        self.assertEqual(result['breadcrumbs'][-1]['path'], result['path'])
        self.assertEqual(result['parent'], str(self.root.resolve()))
        self.assertEqual(sorted(str(path) for path in folder.iterdir()), before)
        self.assertEqual([item['name'] for item in browse_directories(str(folder), query='AL')['directories']], ['Alpha'])
        self.assertEqual(len(browse_directories(str(folder), show_hidden=True)['directories']), 3)
        with self.assertRaises(ValueError):
            browse_directories('relative')
        with self.assertRaises(ValueError):
            browse_directories(str(folder / 'file.txt'))
        with self.assertRaises(OSError):
            browse_directories(str(folder / 'missing'))
        with mock.patch('dev_mesh_console.directories.os.scandir', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                browse_directories(str(folder))

    def test_directory_limit_and_server_side_filter_reach_unlisted_names(self):
        for number in range(DIRECTORY_LIMIT + 2):
            (self.root / f'folder-{number:03}').mkdir()
        result = browse_directories(str(self.root))
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['directories']), DIRECTORY_LIMIT)
        filtered = browse_directories(str(self.root), query=f'folder-{DIRECTORY_LIMIT + 1}')
        self.assertEqual(len(filtered['directories']), 1)
        self.assertFalse(filtered['truncated'])

    def make_state(self):
        return ConsoleState(database=Path(self.temporary.name) / 'observer.sqlite3',
                            registry=RootRegistry(Path(self.temporary.name) / 'roots.json'),
                            max_depth=0, collect_interval=60)

    def test_registration_reports_saved_when_collection_is_busy_or_fails(self):
        state = self.make_state()
        try:
            for failure in (RuntimeError('collection is already running'), OSError('scan unavailable')):
                with mock.patch.object(state, 'collect', side_effect=failure):
                    result = state.add_root(str(self.root))
                self.assertTrue(result['saved'])
                self.assertFalse(result['collection']['refreshed'])
                self.assertEqual(result['roots'], [str(self.root.resolve())])
                self.assertEqual(RootRegistry(state.registry.path).roots(), [self.root.resolve()])
        finally:
            state.close()

    def test_loopback_picker_navigation_registration_and_origin_boundary(self):
        state = self.make_state()
        folder = self.root / '子目录 with spaces'
        folder.mkdir()
        try:
            server = ConsoleServer('127.0.0.1', 0, state)
        except PermissionError:
            state.close()
            self.skipTest('loopback sockets are unavailable in this sandbox')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        def call(method, path, body=None, headers=None):
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            route = '/api/directories?' + urlencode({'path': str(self.root), 'query': '子目录'})
            status, value = call('GET', route)
            self.assertEqual(status, 200)
            self.assertEqual(value['directories'], [{'name': folder.name, 'path': str(folder.resolve())}])
            self.assertEqual(state.registry.roots(), [])
            self.assertEqual(call('GET', route, headers={'Origin': 'https://example.invalid'})[0], 403)
            self.assertEqual(call('GET', route, headers={'Host': 'example.invalid'})[0], 403)
            self.assertEqual(call('GET', '/api/directories?hidden=true')[0], 400)
            self.assertEqual(call('GET', '/api/directories?path=a&path=b')[0], 400)
            status, added = call('POST', '/api/roots', json.dumps({'path': str(folder)}))
            self.assertEqual(status, 200)
            self.assertTrue(added['saved'])
            self.assertTrue(added['collection']['refreshed'])
            self.assertEqual(added['roots'], [str(folder.resolve())])
            self.assertEqual(call('POST', '/api/roots', json.dumps({'path': str(folder)}))[1]['roots'], [str(folder.resolve())])
            self.assertTrue(folder.is_dir())
            self.assertFalse((folder / '.dev-mesh').exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
