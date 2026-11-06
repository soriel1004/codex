"""Per-PC loopback gateway: native Save As and exit of this program's own window.

The NAS database server remains the only writer. Each Windows launcher owns its
gateway and isolated browser process, so a client's Exit cannot stop another PC.
"""
import json
import http.client
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError


def choose_backup_path(filename):
    # Run the native dialog on the PC that clicked Backup, never on the NAS server.
    script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = 'AS 데이터 백업 저장'
$dialog.Filter = 'AS 데이터베이스 (*.db)|*.db'
$dialog.DefaultExt = 'db'
$dialog.AddExtension = $true
$dialog.OverwritePrompt = $true
$dialog.FileName = $env:AS_BACKUP_FILENAME
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.FileName)
    }
} finally { $dialog.Dispose() }
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command', script],
                            env={**os.environ, 'AS_BACKUP_FILENAME':filename},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('백업 저장 위치 선택 창을 열지 못했습니다. Windows PowerShell 사용 가능 여부를 확인하세요.')
    value = result.stdout.decode('utf-8-sig').strip()
    return Path(value) if value else None


class DesktopSession:
    def __init__(self, backend, on_exit=None, picker=choose_backup_path, can_restart=False):
        parsed = urlparse(backend)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('프로그램 서버 주소를 확인하세요.')
        self.backend = backend.rstrip('/')
        self.on_exit = on_exit
        self.picker = picker
        self.can_restart = can_restart
        self.restart_requested = False
        self.transition_requested = False
        self.token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        self.backup_lock = threading.Lock()
        self.browser = None
        self.profile = None
        self.server = None
        self.thread = None
        self.closing = False
        self.close_lock = threading.Lock()

    def start(self, open_browser=True):
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def send(self, value, status=200):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if self.headers.get('Host') != session.authority:
                    self.send({'error':'허용되지 않은 접속입니다.'}, 403); return False
                origin = self.headers.get('Origin')
                if origin and origin != session.origin:
                    self.send({'error':'프로그램 창에서 실행하세요.'}, 403); return False
                cookie = SimpleCookie()
                try: cookie.load(self.headers.get('Cookie', ''))
                except Exception: pass
                if not secrets.compare_digest(cookie.get('as_session').value if cookie.get('as_session') else '', session.token):
                    self.send({'error':'실행 파일로 프로그램을 다시 여세요.'}, 403); return False
                return True

            def do_GET(self):
                parsed = urlparse(self.path)
                token = parse_qs(parsed.query).get('session', [''])[0]
                if parsed.path == '/' and self.headers.get('Host') == session.authority and secrets.compare_digest(token, session.token):
                    self.send_response(303)
                    self.send_header('Location', '/')
                    self.send_header('Set-Cookie', f'as_session={session.token}; HttpOnly; SameSite=Strict; Path=/')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', '0')
                    self.end_headers(); return
                if not self.authorized(): return
                if parsed.path == '/api/program':
                    return self.send({'nativeBackup':True, 'canExit':True, 'canRestart':session.can_restart, 'server':session.on_exit is not None})
                self.proxy()

            def do_POST(self):
                if not self.authorized(): return
                if urlparse(self.path).path == '/api/restore/preview':
                    import backup_restore
                    try: length = int(self.headers.get('Content-Length','0'))
                    except ValueError: length = 0
                    if self.headers.get('Content-Type','').split(';')[0] != 'application/octet-stream' or not 16 <= length <= backup_restore.MAX_UPLOAD:
                        return self.send({'error':'512MB 이하의 AS 백업 파일(.db)을 선택하세요.'},400)
                    if not session.backup_lock.acquire(blocking=False):
                        return self.send({'error':'백업 또는 복원 작업이 진행 중입니다.'},409)
                    try: self.upload_backup(length)
                    finally: session.backup_lock.release()
                    return
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    return self.send({'error':'JSON 형식으로 요청하세요.'}, 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 200000: raise ValueError()
                    body = self.rfile.read(length)
                except ValueError:
                    return self.send({'error':'요청 크기를 확인하세요.'}, 400)
                endpoint = urlparse(self.path).path
                if endpoint == '/api/program/backup':
                    with session.close_lock:
                        if session.transition_requested or session.closing:
                            return self.send({'error':'프로그램을 종료하고 있습니다.'}, 409)
                        if not session.backup_lock.acquire(blocking=False):
                            return self.send({'error':'백업 저장이 진행 중입니다.'}, 409)
                    status = 200
                    try:
                        result = session.save_backup()
                    except (OSError, ValueError) as error:
                        result, status = {'error':str(error)}, 503
                    finally: session.backup_lock.release()
                    self.send(result, status)
                elif endpoint in ('/api/program/exit', '/api/program/restart'):
                    restart = endpoint.endswith('/restart')
                    with session.close_lock:
                        if restart and not session.can_restart:
                            return self.send({'error':'NAS의 start-windows.bat로 프로그램을 다시 실행하세요.'}, 409)
                        if session.closing or session.transition_requested:
                            return self.send({'error':'이미 종료 또는 재실행 중입니다.'}, 409)
                        if session.backup_lock.locked():
                            return self.send({'error':'백업 저장을 마친 뒤 종료하세요.'}, 409)
                        session.restart_requested = restart
                        session.transition_requested = True
                    self.send({'ok':True})
                    threading.Thread(target=session.close, daemon=True).start()
                else:
                    if endpoint == '/api/restore/commit':
                        if not session.backup_lock.acquire(blocking=False):
                            return self.send({'error':'백업 또는 복원 작업이 진행 중입니다.'},409)
                        try: self.proxy(body)
                        finally: session.backup_lock.release()
                    else: self.proxy(body)

            def upload_backup(self, length):
                backend = urlparse(session.backend)
                connection = http.client.HTTPConnection(backend.hostname,backend.port or 80,timeout=900)
                started = False
                try:
                    connection.putrequest('POST','/api/restore/preview')
                    connection.putheader('Content-Type','application/octet-stream')
                    connection.putheader('Content-Length',str(length))
                    connection.putheader('Origin',session.backend)
                    connection.endheaders()
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536,remaining))
                        if not chunk: raise OSError('백업 파일 전송이 중단되었습니다.')
                        connection.send(chunk); remaining -= len(chunk)
                    response = connection.getresponse()
                    self.send_response(response.status)
                    for name in ('Content-Type','Content-Length'):
                        if response.getheader(name): self.send_header(name,response.getheader(name))
                    self.send_header('Cache-Control','no-store'); self.end_headers(); started = True
                    shutil.copyfileobj(response,self.wfile,65536)
                except (OSError,http.client.HTTPException):
                    if not started: self.send({'error':'백업 파일을 서버로 전달하지 못했습니다. 연결을 확인하세요.'},503)
                finally: connection.close()

            def proxy(self, body=None):
                # Fixed upstream; never accept a destination host from a browser request.
                parsed = urlparse(self.path)
                if parsed.scheme or parsed.netloc or parsed.path not in ('/', '/app.js', '/style.css') and not parsed.path.startswith('/api/'):
                    return self.send({'error':'찾을 수 없습니다.'}, 404)
                headers = {'Origin':session.backend} if body is not None else {}
                if body is not None: headers['Content-Type'] = 'application/json'
                request = Request(session.backend + self.path, data=body, headers=headers)
                started = False
                try:
                    try: response = build_opener(ProxyHandler({})).open(request, timeout=2 if parsed.path == '/api/health' else 900 if parsed.path == '/api/restore/commit' else 120)
                    except HTTPError as error: response = error
                    with response:
                        self.send_response(response.status)
                        for name in ('Content-Type', 'Content-Length', 'Content-Disposition'):
                            if response.headers.get(name): self.send_header(name, response.headers[name])
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        started = True
                        shutil.copyfileobj(response, self.wfile, 65536)
                except (BrokenPipeError, ConnectionResetError): pass
                except OSError:
                    if not started: self.send({'error':'서버와 연결할 수 없습니다. 입력 내용을 보관하고 실행 파일을 다시 여세요.'}, 503)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.authority = f'127.0.0.1:{self.server.server_port}'
        self.origin = 'http://' + self.authority
        self.url = self.origin + '/?session=' + self.token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if open_browser:
            try: self.open_window()
            except BaseException:
                self.on_exit = None
                self.close(); raise
        return self.url

    def open_window(self):
        import app
        command = app.windows_browser_command(self.url) if os.name == 'nt' else None
        if not command:
            raise ValueError('종료 버튼을 지원하는 Edge, Chrome 또는 Firefox를 설치한 뒤 다시 실행하세요.')
        base = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'ASManager' / 'windows'
        base.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix='session-', dir=base))
        executable = command[0]
        if Path(executable).name.lower() == 'firefox.exe':
            args = [executable, '-no-remote', '-profile', str(self.profile), '-new-window', self.url]
        else:
            args = [executable, '--user-data-dir=' + str(self.profile), '--no-first-run', '--no-default-browser-check', '--app=' + self.url]
        self.browser = subprocess.Popen(args)

    def save_backup(self):
        from datetime import datetime, timezone, timedelta
        filename = 'AS-backup-' + datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d-%H%M%S') + '.db'
        target = self.picker(filename)
        if target is None: return {'cancelled':True}
        target = Path(target)
        temporary = target.with_name(target.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            request = Request(self.backend + '/api/backup')
            with build_opener(ProxyHandler({})).open(request, timeout=120) as response, temporary.open('xb') as output:
                if response.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    raise ValueError('백업 파일을 받지 못했습니다. 서버 연결을 확인하세요.')
                expected = int(response.headers['Content-Length'])
                shutil.copyfileobj(response, output, 65536)
                output.flush(); os.fsync(output.fileno())
            if temporary.stat().st_size != expected:
                raise ValueError('백업 파일을 끝까지 받지 못했습니다. 다시 시도하세요.')
            with temporary.open('rb') as incoming:
                if incoming.read(16) != b'SQLite format 3\x00': raise ValueError('정상적인 AS 백업 파일이 아닙니다.')
            temporary.replace(target)
        finally: temporary.unlink(missing_ok=True)
        return {'ok':True, 'filename':target.name}

    def wait(self):
        self.stopped.wait()

    def close(self):
        with self.close_lock:
            already_closing = self.closing
            self.closing = True
        if already_closing:
            self.stopped.wait(timeout=15)
            return
        try:
            if self.on_exit: self.on_exit()
        finally:
            try:
                if self.browser and self.browser.poll() is None:
                    try:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(self.browser.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        else: self.browser.terminate()
                    except OSError: self.browser.terminate()
                    try: self.browser.wait(timeout=5)
                    except subprocess.TimeoutExpired: self.browser.kill()
            finally:
                try:
                    if self.server:
                        self.server.shutdown(); self.server.server_close()
                finally:
                    if self.profile: shutil.rmtree(self.profile, ignore_errors=True)
                    self.stopped.set()
