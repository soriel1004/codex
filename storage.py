"""Local SQLite storage for one server; clients use HTTP, never SMB database access."""
import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_initialised = {}
_schema_lock = threading.RLock()
SCHEMA_VERSION = 200
active_sync = None


class SharedStoreError(RuntimeError):
    """A shared durable commit could not be completed safely."""


def is_network_path(path):
    value = str(path)
    if value.startswith(('\\\\', '//')):
        return True
    if os.name == 'nt':
        import ctypes
        drive = Path(path).resolve().anchor
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4
    return False


def require_local(path):
    if is_network_path(path):
        raise ValueError('데이터 폴더는 서버 PC의 로컬 디스크에 두세요. NAS/네트워크 드라이브의 DB를 직접 열 수 없습니다. 다른 PC에서는 서버 주소로 접속하세요.')


def readonly_uri(path):
    uri = path.as_uri()
    # SQLite rejects non-local URI authorities in standard Windows builds.
    # An UNC path in the URI path component works without ALLOW_URI_AUTHORITY.
    if uri.startswith('file://') and not uri.startswith('file:///'):
        uri = 'file:////' + uri[len('file://'):]
    return uri + '?mode=ro'


class Connection(sqlite3.Connection):
    sync = None

    def commit(self):
        if self.sync is not None and self.in_transaction:
            return self.sync.commit(self, super().commit)
        return super().commit()

    def __exit__(self, *args):
        try:
            if args[0] is None:
                try:
                    self.commit()
                except BaseException:
                    self.rollback()
                    raise
            else:
                self.rollback()
            return False
        finally:
            self.close()


def fold(value):
    return unicodedata.normalize('NFKC', str(value or '')).lower()


def compact(value):
    return ''.join(ch for ch in value if not ch.isspace() and unicodedata.category(ch) != 'Pd' and ch != '\u2212')


def search_values(record):
    items, events = record['items'], record.get('returns', [])
    serials = lambda values: [part for value in values for part in re.split('[,，\r\n]+', str(value or ''))]
    fields = {
        'number': [record['number']], 'supplier': [record['supplier']],
        'trackingNumber': [record.get('trackingNumber')],
        'name': [i['name'] for i in items], 'qrCodeNumber': [i.get('qrCodeNumber') for i in items],
        'outboundSerial': serials([i.get('serial') for i in items]),
        'receivedSerial': serials([i.get('receivedSerial') for i in items] + [i.get('receivedSerial') for e in events for i in e['items']]),
        'details': [record.get('note'), record.get('result')] +
            [i.get(key) for i in items for key in ('fault', 'receiptTreatment', 'mbRepairReport')] +
            [e.get(key) for e in events for key in ('result', 'mbRepairReport')],
        'other': [record.get(key) for key in ('date','shippedMonth','courier','phone','company','companyPhone','manager','kind','returned')] +
            [{'부분반환':'부분완료','반환완료':'완료'}.get(record['status'], record['status'])] + record.get('requesters', []) +
            [i.get(key) for i in items for key in ('productionMonth','receivedDate')] + [e['date'] for e in events]
    }
    for field, values in fields.items():
        for text in dict.fromkeys(fold(v) for v in values if v is not None and v != ''):
            yield field, text, compact(text)


def reserve_number(c, number):
    match = re.fullmatch(r'(\d{6})-(\d+)', number)
    if match:
        c.execute('INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                  'SET value=MAX(CAST(app_meta.value AS INTEGER),CAST(excluded.value AS INTEGER))',
                  ('slip_number_max:' + match[1], str(int(match[2]))))


def save_record(c, slip_id, record):
    # Calculated fields are rebuilt on reading; do not duplicate them in JSON.
    raw = {k:v for k,v in record.items() if k not in ('id','elapsedDays','shippedMonth','returned','result')}
    calculated = ('sourceIndex','receivedSerial','mbRepairReport','returnedQty','remainingQty','receiptTreatment','receivedDate','elapsedDays')
    raw['items'] = [{k:v for k,v in i.items() if k not in calculated} for i in record['items']]
    c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(raw, ensure_ascii=False, separators=(',', ':')), slip_id))
    c.execute('UPDATE slips SET number=?,receipt_date=?,status=?,completed=?,supplier=?,requester_group=?,indexed=1 WHERE id=?',
              (record['number'],record['date'],record['status'],int(record['status']=='반환완료'),record['supplier'],
               '|'.join(record.get('requesters', [])),slip_id))
    c.execute('DELETE FROM slip_search WHERE rowid IN (SELECT search_id FROM slip_search_rows WHERE slip_id=?)', (slip_id,))
    c.execute('DELETE FROM slip_search_rows WHERE slip_id=?', (slip_id,))
    for field, value, packed in search_values(record):
        search_id = c.execute('INSERT INTO slip_search_rows(slip_id) VALUES(?)', (slip_id,)).lastrowid
        c.execute('INSERT INTO slip_search(rowid,slip_id,field,value,compact) VALUES(?,?,?,?,?)',
                  (search_id,slip_id,field,value,packed))
    reserve_number(c, record['number'])


def snapshot(source, destination, prefix='as-backup'):
    """Back up through SQLite, then copy the completed snapshot to NAS if requested."""
    source, destination = Path(source).resolve(), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # The backup database itself must be created on the server's local disk.
    local = source.parent / 'backups'
    local.mkdir(parents=True, exist_ok=True)
    name = f'{prefix}-{datetime.now(timezone(timedelta(hours=9))):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.db'
    temporary, completed = local / (name + '.tmp'), local / name
    try:
        with closing(sqlite3.connect(readonly_uri(source), uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256, sleep=0.01)
                dst.commit()
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('백업 무결성 검사에 실패했습니다.')
        temporary.replace(completed)
        if destination.resolve() != local.resolve():
            remote_temp = destination / (name + '.tmp')
            try:
                shutil.copyfile(completed, remote_temp)
                remote_temp.replace(destination / name)
            finally:
                remote_temp.unlink(missing_ok=True)
        return completed
    finally:
        temporary.unlink(missing_ok=True)


def _initialise(c, path, normalise):
    old_version = c.execute('PRAGMA user_version').fetchone()[0]
    existed = bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='slips'").fetchone())
    if existed and old_version < SCHEMA_VERSION and c.execute('SELECT 1 FROM slips LIMIT 1').fetchone():
        snapshot(path, path.parent / 'backups', 'before-V2-migration')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('BEGIN IMMEDIATE')
    try:
        statements = [
            'CREATE TABLE IF NOT EXISTS slips (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, phone TEXT NOT NULL, address TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)',
            'CREATE TABLE IF NOT EXISTS receipt_options (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)',
            'CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)'
        ]
        for statement in statements:
            c.execute(statement)
        columns = {row[1] for row in c.execute('PRAGMA table_info(slips)')}
        for name, kind in [('number','TEXT'),('receipt_date','TEXT'),('status','TEXT'),('completed','INTEGER'),
                           ('supplier','TEXT'),('requester_group','TEXT'),('indexed','INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                c.execute(f'ALTER TABLE slips ADD COLUMN {name} {kind}')
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='slip_search'").fetchone():
            try:
                c.execute("CREATE VIRTUAL TABLE slip_search USING fts5(value,compact,slip_id UNINDEXED,field UNINDEXED,tokenize='trigram')")
            except sqlite3.OperationalError:
                c.execute('CREATE TABLE slip_search(value TEXT,compact TEXT,slip_id INTEGER,field TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS slip_search_rows(search_id INTEGER PRIMARY KEY,slip_id INTEGER NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS search_slip_idx ON slip_search_rows(slip_id)')
        c.execute('CREATE TRIGGER IF NOT EXISTS slips_dirty AFTER UPDATE OF body ON slips BEGIN UPDATE slips SET indexed=0 WHERE id=new.id; END')
        c.execute('CREATE TRIGGER IF NOT EXISTS slips_delete AFTER DELETE ON slips BEGIN '
                  'DELETE FROM slip_search WHERE rowid IN (SELECT search_id FROM slip_search_rows WHERE slip_id=old.id); '
                  'DELETE FROM slip_search_rows WHERE slip_id=old.id; END')
        for name, columns in [('number','number'),('date','receipt_date'),('completed','completed,id'),('status','status,id'),('requester','requester_group,id')]:
            c.execute(f'CREATE INDEX IF NOT EXISTS slips_{name}_idx ON slips({columns})')
        c.execute('CREATE INDEX IF NOT EXISTS slips_dirty_idx ON slips(id) WHERE indexed=0')
        if old_version < SCHEMA_VERSION:
            for slip_id, body in c.execute('SELECT id,body FROM slips').fetchall():
                save_record(c, slip_id, normalise(json.loads(body)))
            c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        c.commit()
    except BaseException:
        c.rollback()
        raise


def connect(data, normalise):
    data = Path(data).resolve()
    require_local(data)
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'as.db'
    c = sqlite3.connect(path, timeout=30, factory=Connection)
    try:
        c.execute('PRAGMA busy_timeout=30000')
        with _schema_lock:
            identity = path.stat().st_ino
            if _initialised.get(str(path)) != identity:
                _initialise(c, path, normalise)
                _initialised[str(path)] = identity
        c.search_fts = 'VIRTUAL TABLE' in c.execute("SELECT sql FROM sqlite_master WHERE name='slip_search'").fetchone()[0].upper()
        # A partial index makes this check constant-sized for ordinary requests.
        dirty = c.execute('SELECT id,body FROM slips WHERE indexed=0').fetchall()
        if dirty:
            c.execute('BEGIN IMMEDIATE')
            # Re-read after obtaining the write lock, so another writer cannot be overwritten.
            for slip_id, body in c.execute('SELECT id,body FROM slips WHERE indexed=0').fetchall():
                save_record(c, slip_id, normalise(json.loads(body)))
            c.commit()
        if active_sync is not None and active_sync.data.resolve() == data:
            active_sync.attach(c)
        return c
    except BaseException:
        c.close()
        raise


def page_records(c, params, normalise):
    def param(name, default=''):
        return params.get(name, [default])[0]
    try:
        page, size = max(1, int(param('page','1'))), int(param('size','50'))
    except ValueError:
        raise ValueError('페이지와 표시 건수를 확인하세요.')
    if size not in (25,50,100):
        raise ValueError('한 페이지는 25·50·100건 중 선택하세요.')
    view, status, requester, field = param('view','active'), param('status'), param('requester'), param('field','all')
    if view not in ('active','completed','all') or field not in ('all','serial','outboundSerial','receivedSerial','number','supplier','trackingNumber','name','qrCodeNumber','details'):
        raise ValueError('목록과 검색 대상을 확인하세요.')
    if status and status not in ('접수','발송','처리중','부분반환','반환완료'):
        raise ValueError('진행상태를 확인하세요.')
    if requester not in ('','GN','대일','none'):
        raise ValueError('의뢰업체를 확인하세요.')
    query = fold(param('q')).strip()
    if len(query) > 500:
        raise ValueError('검색어는 500자 이내로 입력하세요.')
    conditions, args = [], []
    if view != 'all':
        conditions.append('s.completed=?'); args.append(int(view == 'completed'))
    if status:
        conditions.append('s.status=?'); args.append(status)
    if requester:
        conditions.append('s.requester_group IN (?,?)')
        args.extend(('', '') if requester == 'none' else (requester,'GN|대일'))
    for term in query.split():
        packed = compact(term)
        variants = [('value',term)] + ([('compact',packed)] if packed else [])
        # Trigram MATCH accelerates arbitrary substrings; shorter terms use a literal scan.
        if c.search_fts and all(len(value) >= 3 for _,value in variants):
            search = ' OR '.join(column + ':"' + value.replace('"','""') + '"' for column,value in variants)
            expression, values = 'slip_search MATCH ?', [search]
        else:
            expression = '(' + ' OR '.join(f'instr({column},?)>0' for column,_ in variants) + ')'
            values = [value for _,value in variants]
        if field != 'all':
            selected = ('outboundSerial','receivedSerial') if field == 'serial' else (field,)
            expression += ' AND field IN (' + ','.join('?' for _ in selected) + ')'
            values.extend(selected)
        conditions.append('s.id IN (SELECT slip_id FROM slip_search WHERE ' + expression + ')')
        args.extend(values)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    c.execute('BEGIN')  # Count, page and statistics describe the same committed snapshot.
    count = c.execute('SELECT COUNT(*) FROM slips s' + where, args).fetchone()[0]
    pages = max(1, (count + size - 1) // size)
    page = min(page, pages)
    rows = [normalise(dict(json.loads(body),id=slip_id)) for slip_id,body in c.execute(
        'SELECT s.id,s.body FROM slips s' + where + ' ORDER BY s.id DESC LIMIT ? OFFSET ?', args + [size,(page-1)*size])]
    counts = dict(c.execute('SELECT completed,COUNT(*) FROM slips GROUP BY completed'))
    stats = {'total':sum(counts.values()),'active':counts.get(0,0),'completed':counts.get(1,0),
             'suppliers':c.execute('SELECT COUNT(*) FROM suppliers').fetchone()[0]}
    return {'rows':rows,'total':count,'page':page,'pages':pages,'size':size,'stats':stats}


class ServerLock:
    """An OS lock is released after a crash; a stale filename never blocks startup."""
    def __init__(self, data):
        self.path = Path(data) / '.server.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.file = self.path.open('x+b')
        except FileExistsError:
            self.file = self.path.open('r+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0,2)
                if not self.file.tell():
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('이 데이터 폴더를 사용하는 서버가 이미 실행 중입니다. 다른 PC에서는 서버 주소로 접속하세요.')
        return self

    def __exit__(self, *args):
        self.file.close()
