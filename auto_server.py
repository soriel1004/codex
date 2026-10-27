"""First-starter NAS election, fenced durable change journal and local SQLite caches.

Only small immutable commit files and completed checkpoints live on SMB. SQLite
always runs locally. A fresh NAS byte lock fences every published commit; a
timeout or unreachable server is never used to steal the lifetime leader lock.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler
import storage

PROTOCOL = 1
TABLES = {
    'slips':('id','body'), 'settings':('id','body'),
    'suppliers':('id','name','phone','address'), 'products':('id','name'),
    'receipt_options':('id','name'), 'app_meta':('key','value')
}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_bytes(path, content, exclusive=False, before_publish=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(content); output.flush(); os.fsync(output.fileno())
        if before_publish: before_publish()
        if exclusive:
            if os.name == 'nt':
                os.rename(temporary,path)  # Windows rename does not replace an existing file.
            else:
                os.link(temporary,path)
                temporary.unlink()
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, content, **options):
    atomic_bytes(path,json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'),**options)


def read_owner(shared):
    claims = sorted((shared/'owners').glob('[0-9]'*20+'.json'))
    if not claims: raise FileNotFoundError('No elected server yet')
    claim = read_json(claims[-1])
    state = shared/'states'/(claim['token']+'.json')
    if state.exists():
        published = read_json(state)
        if published.get('token') == claim['token'] and published.get('generation') == claim['generation']:
            return published
    return claim


def copy_checked(source, target, expected=None):
    """Publish a complete file, with no SQLite connection to the shared path."""
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    digest = hashlib.sha256()
    try:
        with source.open('rb') as incoming, temporary.open('xb') as output:
            while chunk := incoming.read(1024*1024):
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        checksum = digest.hexdigest()
        if expected is not None and checksum != expected:
            raise storage.SharedStoreError('NAS 데이터의 검사값이 맞지 않습니다. 기존 파일을 보존하고 백업을 확인하세요.')
        temporary.replace(target)
        return checksum
    finally:
        temporary.unlink(missing_ok=True)


def packed(payload):
    return json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def advertised_host(root):
    override = os.environ.get('AS_ADVERTISE_HOST')
    if override:
        return override
    nas = urlparse(root.as_uri()).hostname
    if nas:
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
                route.connect((nas,445))
                return route.getsockname()[0]
        except OSError:
            pass
    return socket.gethostbyname(socket.gethostname())


class SharedServer:
    def __init__(self, root, local_base=None):
        self.root = Path(root).resolve()
        self.shared = self.root / '.as-shared'
        self.leader_lock = storage.ServerLock(self.shared/'leader')
        self.claimed = False
        self.token = uuid.uuid4().hex
        self.identity = None
        self.data = None
        self.local_base = Path(local_base or Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'ASManager' / 'auto')
        self.sequence = 0
        self.checkpoint_sequence = 0
        self.failed = False
        self.mutex = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = None
        self.url = ''
        self.prepared = False
        self.generation = 0

    def claim(self):
        try:
            self.leader_lock.__enter__()
        except ValueError:
            return False
        self.claimed = True
        return True

    @contextmanager
    def publication(self, timeout=30):
        deadline = time.monotonic()+timeout
        while True:
            lock = storage.ServerLock(self.shared/'publication')
            try:
                lock.__enter__()
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise storage.SharedStoreError('NAS 저장 잠금을 기다리는 시간이 초과되었습니다. 저장을 다시 확인하세요.')
                time.sleep(.05)
        try:
            yield
        finally:
            lock.__exit__(None,None,None)

    def assert_owner(self):
        if self.failed:
            raise storage.SharedStoreError('이 서버의 처리를 중단했습니다. 입력 내용을 보관하고 프로그램을 종료 후 다시 실행하세요.')
        try:
            owner = read_owner(self.shared)
            if owner.get('token') != self.token or owner.get('share_id') != self.identity['share_id']:
                raise storage.SharedStoreError('서버가 다른 PC로 변경되었습니다. 입력 내용을 보관한 뒤 실행 파일을 다시 여세요.')
        except (OSError,ValueError,TypeError) as error:
            raise storage.SharedStoreError('NAS에 연결할 수 없어 저장을 중단했습니다. 네트워크 연결 후 다시 시도하세요.') from error

    def prove_lease(self):
        # SMB byte-range locks also fence writes to this byte after a reconnect.
        try:
            handle = self.leader_lock.file
            handle.seek(0); handle.write(b'1'); handle.flush(); os.fsync(handle.fileno())
        except (OSError,ValueError,AttributeError) as error:
            raise storage.SharedStoreError('NAS 서버 잠금을 확인하지 못했습니다. 이 서버에서 저장을 중단합니다.') from error

    def owner(self, state):
        return {'protocol':PROTOCOL,'share_id':self.identity['share_id'],'token':self.token,
                'pc':socket.gethostname(),'url':self.url,'state':state,'generation':self.generation}

    def publish_state(self, state):
        atomic_json(self.shared/'states'/(self.token+'.json'),self.owner(state),before_publish=self.assert_owner)

    def latest_checkpoint(self):
        paths = list((self.shared/'manifests').glob('*.json'))
        if not paths: return None
        if any(not re.fullmatch(r'[0-9]{20}-[0-9]{20}-[0-9a-f]{32}\.json',path.name) for path in paths):
            raise storage.SharedStoreError('NAS 체크포인트 목록의 형식을 확인하세요.')
        manifest = read_json(max(paths,key=lambda path:(int(path.name[:20]),path.name)))
        if (not isinstance(manifest,dict) or manifest.get('protocol') != PROTOCOL or
            manifest.get('share_id') != self.identity['share_id'] or type(manifest.get('sequence')) is not int or manifest['sequence'] < 0 or
            not re.fullmatch(r'checkpoint-[0-9]{20}-[0-9a-f]{32}\.db',manifest.get('file','')) or
            not re.fullmatch('[0-9a-f]{64}',manifest.get('sha256',''))):
            raise storage.SharedStoreError('NAS 체크포인트 정보를 확인하세요.')
        return manifest

    def durable_tail(self):
        highest = 0
        for path in (self.shared/'tails').glob('*.json'):
            tail = read_json(path)
            if tail.get('share_id') != self.identity['share_id'] or type(tail.get('sequence')) is not int:
                raise storage.SharedStoreError('NAS 저장 완료 순번을 확인하세요.')
            highest = max(highest,tail['sequence'])
        return highest

    def prepare(self, import_initial=None):
        if not self.claimed:
            raise storage.SharedStoreError('서버 잠금을 확보하지 못했습니다.')
        with self.publication():
            identity_path = self.shared/'identity.json'
            if identity_path.exists():
                self.identity = read_json(identity_path)
                if self.identity.get('protocol') != PROTOCOL or not re.fullmatch('[0-9a-f]{32}',self.identity.get('share_id','')):
                    raise storage.SharedStoreError('NAS 공동 사용 데이터의 형식을 확인하세요. 폴더를 삭제하거나 새로 만들지 마세요.')
            else:
                if list((self.shared/'manifests').glob('*.json')) or list((self.shared/'transactions').glob('*.json')):
                    raise storage.SharedStoreError('NAS 데이터 식별 파일이 없습니다. 남은 데이터를 초기화하지 않습니다.')
                self.identity = {'protocol':PROTOCOL,'share_id':uuid.uuid4().hex,'initialised':False}
                atomic_json(identity_path,self.identity)
            claims = list((self.shared/'owners').glob('[0-9]'*20+'.json'))
            self.generation = max((int(path.stem) for path in claims),default=0)+1
            self.prove_lease()
            atomic_json(self.shared/'owners'/f'{self.generation:020d}.json',self.owner('starting'),exclusive=True)
        base = self.local_base.resolve()/self.identity['share_id']
        storage.require_local(base)
        self.data = base / ('cache-' + uuid.uuid4().hex)
        self.data.mkdir(parents=True)
        manifest = self.latest_checkpoint()
        if manifest is not None:
            if manifest.get('share_id') != self.identity['share_id'] or not re.fullmatch(r'checkpoint-[0-9]{20}-[0-9a-f]{32}\.db',manifest.get('file','')):
                raise storage.SharedStoreError('NAS 체크포인트 정보를 확인하세요.')
            copy_checked(self.shared/'checkpoints'/manifest['file'],self.data/'as.db',manifest['sha256'])
            self.sequence = int(manifest['sequence'])
            self.replay()
        else:
            if self.identity.get('initialised') or list((self.shared/'transactions').glob('*.json')):
                raise storage.SharedStoreError('기존 NAS 데이터 파일이 없습니다. 빈 데이터로 대체하지 않습니다.')
            if import_initial:
                import_initial(self.data/'as.db')
        import app
        app.DATA = self.data
        with app.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS auto_state(sequence INTEGER NOT NULL)')
            c.execute('DELETE FROM auto_state')
            c.execute('INSERT INTO auto_state VALUES(?)',(self.sequence,))
        # Initial seeding is included in the first checkpoint, not a large first-use journal.
        app.suppliers(); app.products(); app.receipt_option_rows()
        self.checkpoint(force=True)
        self.identity['initialised'] = True
        with self.publication():
            self.assert_owner()
            atomic_json(self.shared/'identity.json',self.identity)
        self.prepared = True
        # These are replaceable local caches; keep two older sessions for inspection.
        for old in sorted(base.glob('cache-*'),key=lambda path:path.stat().st_mtime,reverse=True)[3:]:
            if old != self.data:
                try: shutil.rmtree(old)
                except OSError: pass

    def replay(self):
        with closing(sqlite3.connect(self.data/'as.db')) as c:
            state = c.execute('SELECT sequence FROM auto_state').fetchone()
            if not state or state[0] != self.sequence:
                raise storage.SharedStoreError('NAS 체크포인트 순번이 맞지 않습니다. 원본을 보존하고 백업을 확인하세요.')
            for path in sorted((self.shared/'transactions').glob('*.json')):
                if not re.fullmatch(r'[0-9]{20}\.json',path.name):
                    raise storage.SharedStoreError('NAS 변경 기록의 파일명이 잘못되었습니다.')
                sequence = int(path.stem)
                if sequence <= self.sequence:
                    continue
                envelope = read_json(path)
                payload = envelope['payload']
                if sequence != self.sequence+1 or payload.get('protocol') != PROTOCOL or payload.get('sequence') != sequence or payload.get('share_id') != self.identity['share_id'] or hashlib.sha256(packed(payload)).hexdigest() != envelope.get('sha256'):
                    raise storage.SharedStoreError('NAS 변경 기록이 누락되었거나 손상되었습니다. 이전 자료로 임의 복원하지 않습니다.')
                self.apply(c,payload['changes'])
                c.execute('UPDATE auto_state SET sequence=?',(sequence,))
                c.commit()
                self.sequence = sequence
            if self.sequence < self.durable_tail():
                raise storage.SharedStoreError('저장 완료된 NAS 변경 기록이 누락되었습니다. 이전 데이터로 시작하지 않습니다.')

    @staticmethod
    def apply(c, changes):
        # Remove changed keys first to support unique-name changes in a transaction.
        for change in changes:
            table = change['table']
            if table not in TABLES:
                raise storage.SharedStoreError('알 수 없는 NAS 변경 기록입니다.')
            key = TABLES[table][0]
            c.execute(f'DELETE FROM {table} WHERE {key}=?',(change['key'],))
        for change in changes:
            if change['row'] is not None:
                columns = TABLES[change['table']]
                if set(change['row']) != set(columns):
                    raise storage.SharedStoreError('NAS 변경 기록의 항목을 확인하세요.')
                c.execute(f'INSERT INTO {change["table"]}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',
                          [change['row'][name] for name in columns])

    def attach(self, c):
        # No uniqueness constraint here: an outer UPSERT can override a trigger's
        # OR IGNORE policy. Deduplicate after all changes have completed instead.
        c.execute('CREATE TEMP TABLE sync_changes(table_name TEXT,row_key)')
        for table, columns in TABLES.items():
            for operation, source in [('INSERT','new'),('UPDATE','new'),('DELETE','old')]:
                c.execute(f'CREATE TEMP TRIGGER sync_{table}_{operation} AFTER {operation} ON main.{table} BEGIN '
                          f"INSERT INTO sync_changes VALUES('{table}',{source}.{columns[0]}); END")
        c.sync = self

    def commit(self, c, local_commit):
        dirty = c.execute('SELECT DISTINCT table_name,row_key FROM sync_changes ORDER BY table_name,row_key').fetchall()
        if not dirty:
            return local_commit()
        with self.mutex:
            try:
                with self.publication():
                    self.assert_owner()
                    changes = []
                    for table, key in dirty:
                        columns = TABLES[table]
                        row = c.execute(f'SELECT {",".join(columns)} FROM {table} WHERE {columns[0]}=?',(key,)).fetchone()
                        changes.append({'table':table,'key':key,'row':dict(zip(columns,row)) if row is not None else None})
                    sequence = self.sequence+1
                    payload = {'protocol':PROTOCOL,'share_id':self.identity['share_id'],'sequence':sequence,'token':self.token,'changes':changes}
                    path = self.shared/'transactions'/f'{sequence:020d}.json'
                    if path.exists():
                        self.failed = True
                        raise storage.SharedStoreError('NAS 저장 순번이 중복되었습니다. 서버를 종료하고 다시 실행하세요.')
                    self.prove_lease()
                    try:
                        atomic_json(path,{'payload':payload,'sha256':hashlib.sha256(packed(payload)).hexdigest()},exclusive=True,before_publish=self.assert_owner)
                    except OSError as error:
                        try: uncertain = path.exists()
                        except OSError: uncertain = True
                        if uncertain:
                            self.failed = True
                            raise storage.SharedStoreError('NAS 저장 결과를 확정하지 못했습니다. 재입력하지 말고 프로그램을 재실행해 저장 결과를 확인하세요.') from error
                        raise
                    # From publication onward, this transaction must survive a local crash.
                    self.sequence = sequence
                    try:
                        atomic_json(self.shared/'tails'/f'{self.generation:020d}.json',{'share_id':self.identity['share_id'],'sequence':sequence},before_publish=self.assert_owner)
                        c.execute('UPDATE auto_state SET sequence=?',(sequence,))
                        c.execute('DELETE FROM sync_changes')
                        local_commit()
                    except BaseException as error:
                        self.failed = True
                        raise storage.SharedStoreError('NAS에는 저장되었지만 이 PC의 처리를 마치지 못했습니다. 재입력하지 말고 프로그램을 재실행해 저장 결과를 확인하세요.') from error
            except (OSError,sqlite3.Error) as error:
                raise storage.SharedStoreError('NAS에 기록하지 못해 저장을 중단했습니다. 연결과 여유 공간을 확인한 뒤 다시 시도하세요.') from error
        if self.sequence-self.checkpoint_sequence >= 200:
            self.wake.set()

    def checkpoint(self, force=False):
        if self.data is None or (not force and self.sequence == self.checkpoint_sequence):
            return
        with self.mutex:
            self.assert_owner()
            source = storage.snapshot(self.data/'as.db',self.data/'backups','auto-checkpoint')
            try:
                checkpoint_id = uuid.uuid4().hex
                filename = f'checkpoint-{self.sequence:020d}-{checkpoint_id}.db'
                checksum = copy_checked(source,self.shared/'checkpoints'/filename)
                with self.publication():
                    self.prove_lease(); self.assert_owner()
                    old_paths = sorted((self.shared/'manifests').glob('*.json'),reverse=True)
                    previous_path = old_paths[0] if old_paths else None
                    previous = read_json(previous_path) if previous_path else None
                    manifest = {'protocol':PROTOCOL,'share_id':self.identity['share_id'],'sequence':self.sequence,'file':filename,'sha256':checksum}
                    atomic_json(self.shared/'manifests'/f'{self.sequence:020d}-{self.generation:020d}-{checkpoint_id}.json',manifest,exclusive=True,before_publish=self.assert_owner)
                    self.checkpoint_sequence = self.sequence
                    # Only delete already-known older generations; never touch a future leader's files.
                    for old in old_paths[1:]:
                        old_file = read_json(old)['file']
                        old.unlink()
                        (self.shared/'checkpoints'/old_file).unlink(missing_ok=True)
                    cutoff = previous['sequence'] if previous else 0
                    for old in (self.shared/'transactions').glob('*.json'):
                        if re.fullmatch('[0-9]{20}',old.stem) and int(old.stem) <= cutoff: old.unlink()
            finally:
                source.unlink(missing_ok=True)

    def activate(self):
        storage.active_sync = self
        def run():
            while not self.stop.is_set():
                self.wake.wait(2); self.wake.clear()
                if not self.stop.is_set() and self.sequence-self.checkpoint_sequence >= 200:
                    try: self.checkpoint()
                    except (OSError,ValueError,sqlite3.Error,storage.SharedStoreError) as error:
                        print('NAS 체크포인트 재시도 예정:',error,flush=True)
        self.worker = threading.Thread(target=run,name='shared-checkpoint',daemon=True)
        self.worker.start()

    def ready(self, port):
        host = advertised_host(self.root)
        self.url = f'http://{host}:{port}'
        with self.publication():
            self.assert_owner()
            self.prove_lease()
            self.publish_state('ready')
        print('이 PC가 자동 서버입니다. 다른 PC 접속:',self.url,flush=True)

    def close(self, clean=True):
        if not self.claimed: return
        self.stop.set(); self.wake.set()
        if self.worker: self.worker.join(timeout=5)
        if storage.active_sync is self: storage.active_sync = None
        try:
            if clean and self.prepared and not self.failed:
                self.checkpoint()
            if self.identity is not None:
                with self.publication(timeout=2):
                    self.assert_owner()
                    self.publish_state('stopped')
        except (OSError,ValueError,sqlite3.Error,storage.SharedStoreError) as error:
            print('서버 종료 안내:',error,flush=True)
        finally:
            if self.claimed:
                self.leader_lock.__exit__(None,None,None)
                self.claimed = False


def client_url(root, timeout=90, try_claim=None):
    shared = Path(root)/'.as-shared'
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if try_claim is not None and try_claim(): return None
        try:
            owner = read_owner(shared)
            identity = read_json(shared/'identity.json')
            if owner.get('state') == 'ready':
                url = owner.get('url','')
                parsed = urlparse(url)
                if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
                    raise storage.SharedStoreError('자동 서버 주소의 형식을 확인하세요.')
                try:
                    with build_opener(ProxyHandler({})).open(url+'/api/health',timeout=3) as response:
                        health = json.load(response)
                    if health.get('share_id') == identity.get('share_id') == owner.get('share_id') and health.get('leader_token') == owner.get('token'):
                        return url
                except (OSError,ValueError) as error:
                    if try_claim is not None and try_claim(): return None
                    raise storage.SharedStoreError('서버 PC('+owner.get('pc','')+')가 실행 중이지만 접속할 수 없습니다. 방화벽·절전·네트워크를 확인하세요. 잠금을 강제로 해제하지 않습니다.') from error
                raise storage.SharedStoreError('서버 식별 정보가 맞지 않습니다. 기존 서버를 확인하세요.')
        except FileNotFoundError:
            pass
        time.sleep(.2)
    raise storage.SharedStoreError('기존 서버가 준비 중이거나 종료되지 않았습니다. 서버 PC를 확인하세요. 중복 서버를 강제로 만들지 않습니다.')


def run(root, import_initial=None, local_base=None, open_browser=True):
    import app
    coordinator = SharedServer(root,local_base)
    if not coordinator.claim():
        url = client_url(root,try_claim=coordinator.claim)
        if url is not None:
            print('기존 서버에 클라이언트로 접속:',url,flush=True)
            if open_browser: app.open_program_window(url)
            return 'client'
    server = None
    try:
        coordinator.prepare(import_initial)
        port = int(os.environ.get('AS_AUTO_PORT','8000'))
        server = app.ThreadingHTTPServer(('0.0.0.0',port),app.Handler)
        coordinator.activate()
        os.environ['AS_HOST']='0.0.0.0'
        os.environ['PORT']=str(server.server_port)
        os.environ['AS_BACKUP_DIR']=str(Path(root)/'backups')
        print('서버 준비 완료. 서버 창을 종료하면 다른 PC에서 다음 실행 시 인계합니다.',flush=True)
        app.main(prepared_server=server,on_ready=coordinator.ready,open_browser=open_browser)
        return 'server'
    finally:
        if server: server.server_close()
        coordinator.close()
