import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler
from unittest.mock import patch
from types import SimpleNamespace

import app
import desktop_session


class DesktopWindows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data = app.DATA
        app.DATA = Path(self.temp.name) / 'server'
        with app.db(): pass
        self.backend = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.worker = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.worker.start()
        self.origin = f'http://127.0.0.1:{self.backend.server_port}'
        self.sessions = []

    def tearDown(self):
        for session in self.sessions: session.close()
        if self.worker.is_alive(): self.backend.shutdown()
        self.backend.server_close(); self.worker.join(timeout=3)
        app.DATA = self.previous_data
        self.temp.cleanup()

    def session(self, **kwargs):
        session = desktop_session.DesktopSession(self.origin, **kwargs)
        self.sessions.append(session)
        url = session.start(open_browser=False)
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        opener.open(url).close()
        return session, opener

    def post(self, session, opener, endpoint, origin=None):
        request = Request(session.origin + endpoint, data=b'{}',
                          headers={'Content-Type':'application/json', 'Origin':origin or session.origin})
        with opener.open(request, timeout=5) as response: return json.load(response)

    def test_backup_is_saved_to_client_selected_path_with_cancel_and_failure_preserving_file(self):
        destination = Path(self.temp.name) / '선택한 폴더'
        destination.mkdir()
        target = destination / '내 AS 백업.db'
        session, opener = self.session(picker=lambda _: target)
        result = self.post(session, opener, '/api/program/backup')
        self.assertTrue(result['ok'])
        with sqlite3.connect(target) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='slips'").fetchone())
        original = target.read_bytes()
        session.picker = lambda _: None
        self.assertEqual(self.post(session, opener, '/api/program/backup'), {'cancelled':True})
        self.assertEqual(target.read_bytes(), original)
        session.picker = lambda _: target
        with patch('desktop_session.shutil.copyfileobj', side_effect=OSError('저장 실패')):
            with self.assertRaises(HTTPError) as failed: self.post(session, opener, '/api/program/backup')
            self.assertEqual(failed.exception.code, 503)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(destination.glob('*.tmp')), [])

    def test_client_exit_closes_only_its_gateway_and_server_exit_stops_backend(self):
        client, opener = self.session()
        other, other_opener = self.session()
        self.assertTrue(self.post(client, opener, '/api/program/exit')['ok'])
        self.assertTrue(client.stopped.wait(3))
        with other_opener.open(other.origin + '/api/health') as response: self.assertTrue(json.load(response)['ok'])
        server, server_opener = self.session(on_exit=self.backend.shutdown)
        with server_opener.open(server.origin + '/api/program') as response: self.assertTrue(json.load(response)['server'])
        self.post(server, server_opener, '/api/program/exit')
        self.assertTrue(server.stopped.wait(3))
        self.worker.join(timeout=3)
        self.assertFalse(self.worker.is_alive())

    def test_gateway_rejects_other_sites_and_unauthenticated_shutdown(self):
        session, opener = self.session()
        with self.assertRaises(HTTPError) as rejected:
            self.post(session, opener, '/api/program/exit', origin='https://unrelated.example')
        self.assertEqual(rejected.exception.code, 403)
        anonymous = build_opener(ProxyHandler({}))
        with self.assertRaises(HTTPError) as rejected:
            self.post(session, anonymous, '/api/program/exit')
        self.assertEqual(rejected.exception.code, 403)
        self.assertFalse(session.closing)

    def test_forwarded_settings_write_uses_server_data(self):
        session, opener = self.session()
        data = {'company':'공동 업체'}
        request = Request(session.origin + '/api/settings', data=json.dumps(data).encode(),
                          headers={'Content-Type':'application/json', 'Origin':session.origin})
        with opener.open(request) as response: self.assertTrue(json.load(response)['ok'])
        with opener.open(session.origin + '/api/settings') as response: self.assertEqual(json.load(response), data)
        self.assertTrue(self.worker.is_alive())

    def test_browser_uses_its_own_profile_and_exit_targets_only_its_process(self):
        session, _ = self.session()
        fake_os = SimpleNamespace(name='nt', environ={**os.environ, 'LOCALAPPDATA':self.temp.name})
        with patch.object(desktop_session, 'os', fake_os), \
             patch.object(app, 'windows_browser_command', return_value=['msedge.exe','--new-window',session.url]), \
             patch.object(desktop_session.subprocess, 'Popen') as popen, \
             patch.object(desktop_session.subprocess, 'run') as run:
            browser = popen.return_value
            browser.pid = 4242
            browser.poll.return_value = None
            session.open_window()
            args = popen.call_args.args[0]
            self.assertIn('--app=' + session.url, args)
            self.assertIn('--user-data-dir=' + str(session.profile), args)
            self.assertTrue(session.profile.is_dir())
            session.close()
            self.assertEqual(run.call_args.args[0], ['taskkill','/PID','4242','/T','/F'])
            self.assertFalse(session.profile.exists())


if __name__ == '__main__': unittest.main()
