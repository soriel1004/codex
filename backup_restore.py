"""Validate SQLite backups in isolation and restore application rows atomically."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
import storage

MAX_UPLOAD = 512 * 1024 * 1024
TABLES = {
    'slips':('id','body'), 'settings':('id','body'),
    'suppliers':('id','name','phone','address'), 'products':('id','name'),
    'receipt_options':('id','name'), 'app_meta':('key','value')
}
lock = threading.RLock()


class RestoreConflict(ValueError): pass


def fingerprint(c):
    digest = hashlib.sha256()
    for table, columns in TABLES.items():
        digest.update(table.encode())
        for row in c.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY {columns[0]}'):
            digest.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def summary(c):
    return {**{table:c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES if table != 'app_meta'},
            'firstDate':c.execute('SELECT MIN(receipt_date) FROM slips').fetchone()[0],
            'lastDate':c.execute('SELECT MAX(receipt_date) FROM slips').fetchone()[0]}


def validate_slip(body, id):
    import app
    r = json.loads(body)
    if not isinstance(r,dict) or not isinstance(r.get('items'),list) or not 1 <= len(r['items']) <= 8:
        raise ValueError('전표 품목 형식을 확인하세요.')
    if not all(isinstance(r.get(key),str) and r[key] for key in ('number','date','supplier','kind','status')):
        raise ValueError('전표의 필수 항목을 확인하세요.')
    app.valid_date(r['date'])
    if type(r.get('version',0)) is not int or r.get('version',0) < 0:
        raise ValueError('전표 수정 버전을 확인하세요.')
    if r['status'] not in ('접수','발송','처리중','부분반환','반환완료'):
        raise ValueError('전표 상태를 확인하세요.')
    for item in r['items']:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not item['name'].strip():
            raise ValueError('품명을 확인하세요.')
        if isinstance(item.get('qty'),bool) or not str(item.get('qty','')).isdigit() or not 1 <= int(item['qty']) <= 99999:
            raise ValueError('품목 수량을 확인하세요.')
        for key in ('serial','fault','note','qrCodeNumber','productionMonth','model'):
            if key in item and not isinstance(item[key],str): raise ValueError('품목 입력 형식을 확인하세요.')
    r = app.normalize(dict(r,id=id))
    for event in r['returns']:
        app.valid_date(event['date'])
        if event['date'] < r['date']: raise ValueError('입고일이 출고일보다 빠릅니다.')
        for item in event['items']:
            if not isinstance(item.get('repairContent',''),str) or len(item.get('repairContent','')) > 2000:
                raise ValueError('품목별 처리내용(수리내용) 형식을 확인하세요.')
            if type(item['index']) is not int or not 0 <= item['index'] < len(r['items']) or type(item['qty']) is not int or item['qty'] <= 0:
                raise ValueError('입고 수량과 품목을 확인하세요.')
    if any(i['remainingQty'] < 0 for i in r['items']): raise ValueError('입고수량이 출고수량을 초과합니다.')
    return r


def safe_database(source, destination):
    """Read data only; never install an uploaded database's schema or triggers."""
    import app
    with closing(sqlite3.connect(storage.readonly_uri(source)+'&immutable=1',uri=True)) as src:
        src.execute('PRAGMA trusted_schema=OFF')
        if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('백업 파일 무결성 검사에 실패했습니다.')
        if src.execute('PRAGMA user_version').fetchone()[0] > storage.SCHEMA_VERSION:
            raise ValueError('더 새로운 형식의 백업입니다. 프로그램을 최신 버전으로 업데이트하세요.')
        schemas = {name:(kind,sql or '') for name,kind,sql in src.execute('SELECT name,type,sql FROM sqlite_master')}
        if not all(table in schemas for table in ('slips','settings')):
            raise ValueError('AS 프로그램에서 만든 데이터베이스 백업을 선택하세요.')
        with storage.connect(destination,app.normalize) as dst:
            for table, columns in TABLES.items():
                if table not in schemas: continue
                kind, sql = schemas[table]
                if kind != 'table' or not re.match(r'CREATE\s+TABLE\b',sql,re.I):
                    raise ValueError('백업의 데이터 테이블 형식을 확인하세요.')
                available = {row[1] for row in src.execute(f'PRAGMA table_info({table})')}
                required = set(columns) - ({'address'} if table == 'suppliers' else set())
                if not required <= available: raise ValueError('백업의 필수 데이터 열이 없습니다.')
                selected = [name if name in available else "'' AS address" for name in columns]
                for row in src.execute(f'SELECT {",".join(selected)} FROM {table}'):
                    if table == 'slips':
                        id, body = row
                        if type(id) is not int or id <= 0: raise ValueError('전표 ID를 확인하세요.')
                        record = validate_slip(body,id)
                        dst.execute('INSERT INTO slips(id,body) VALUES(?,?)',(id,'{}'))
                        storage.save_record(dst,id,record)
                    else:
                        if table == 'app_meta':
                            if not all(isinstance(v,str) for v in row): raise ValueError('목록 설정 형식을 확인하세요.')
                        elif table == 'settings':
                            if row[0] != 1 or not isinstance(json.loads(row[1]),dict): raise ValueError('보낸이 설정을 확인하세요.')
                        elif type(row[0]) is not int or row[0] <= 0 or not all(isinstance(v,str) for v in row[1:]) or not row[1].strip():
                            raise ValueError('등록 목록 형식을 확인하세요.')
                        verb = 'INSERT OR REPLACE' if table == 'app_meta' else 'INSERT'
                        dst.execute(f'{verb} INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',tuple(row))
            # Preserve numbering and ID high-water marks, including deleted records.
            if 'sqlite_sequence' in schemas:
                for table, seq in src.execute('SELECT name,seq FROM sqlite_sequence'):
                    if table in TABLES and type(seq) is int:
                        bump_sequence(dst,table,seq)
            if not dst.execute("SELECT 1 FROM app_meta WHERE key='suppliers_seeded'").fetchone():
                for (body,) in dst.execute('SELECT body FROM slips ORDER BY id DESC'):
                    r=json.loads(body)
                    dst.execute('INSERT OR IGNORE INTO suppliers(name,phone,address) VALUES(?,?,?)',(r['supplier'],r.get('phone',''),''))
                dst.execute("INSERT OR REPLACE INTO app_meta VALUES('suppliers_seeded','1')")
            if not dst.execute("SELECT 1 FROM app_meta WHERE key='products_seeded'").fetchone():
                for (body,) in dst.execute('SELECT body FROM slips ORDER BY id DESC'):
                    for item in json.loads(body)['items']: dst.execute('INSERT OR IGNORE INTO products(name) VALUES(?)',(item['name'],))
                dst.execute("INSERT OR REPLACE INTO app_meta VALUES('products_seeded','1')")
            app.receipt_option_rows(dst)
            if dst.execute('SELECT number FROM slips GROUP BY number HAVING COUNT(*)>1 LIMIT 1').fetchone():
                raise ValueError('백업에 중복 전표번호가 있습니다. 다른 백업을 선택하세요.')
            # Old backups can omit app_meta; reserve their issued slip numbers too.
            for (number,) in dst.execute('SELECT number FROM slips'): storage.reserve_number(dst,number)
            return summary(dst)


def bump_sequence(c, table, sequence):
    current = c.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(table,)).fetchone()
    if current: c.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?',(sequence,table))
    else: c.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(table,sequence))


def stage(stream, length):
    import app
    if not 16 <= length <= MAX_UPLOAD: raise ValueError('16바이트~512MB의 AS 데이터베이스 백업(.db)을 선택하세요.')
    base = app.DATA / 'restore-staging'
    base.mkdir(parents=True,exist_ok=True)
    with lock:
        for old in base.iterdir():
            if re.fullmatch('[0-9a-f]{32}',old.name) and old.is_dir() and old.stat().st_mtime < time.time()-1800: shutil.rmtree(old,ignore_errors=True)
        token = uuid.uuid4().hex
        folder = base/token; folder.mkdir()
        source = folder/'uploaded.db'
        try:
            with source.open('xb') as output:
                remaining = length
                while remaining:
                    chunk = stream.read(min(65536,remaining))
                    if not chunk: raise ValueError('백업 파일 전송이 끝나지 않았습니다. 다시 선택하세요.')
                    output.write(chunk); remaining -= len(chunk)
            with source.open('rb') as incoming:
                if incoming.read(16) != b'SQLite format 3\x00': raise ValueError('AS 데이터베이스 백업(.db)을 선택하세요. 엑셀은 복원할 수 없습니다.')
            restored = safe_database(source,folder/'prepared')
            source.unlink()
            with app.db() as c:
                c.execute('BEGIN')
                current, stamp = summary(c), fingerprint(c)
            info = {'token':token,'backup':restored,'current':current,'stamp':stamp,'created':time.time()}
            (folder/'preview.json').write_text(json.dumps(info,ensure_ascii=False),encoding='utf-8')
            return {key:info[key] for key in ('token','backup','current')}
        except (sqlite3.Error, KeyError, TypeError, IndexError, OverflowError, AttributeError, RecursionError) as error:
            shutil.rmtree(folder,ignore_errors=True)
            raise ValueError('손상되었거나 지원하지 않는 AS 백업 파일입니다. 다른 백업을 선택하세요.') from error
        except BaseException:
            shutil.rmtree(folder,ignore_errors=True)
            raise


def commit(token):
    import app
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token): raise ValueError('백업 파일을 다시 선택하세요.')
    folder = app.DATA/'restore-staging'/token
    with lock:
        manifest = folder/'preview.json'
        if not manifest.is_file(): raise ValueError('복원 준비 정보가 없습니다. 백업 파일을 다시 선택하세요.')
        info = json.loads(manifest.read_text(encoding='utf-8'))
        if time.time()-info['created'] > 1800: raise ValueError('복원 준비가 만료되었습니다. 백업 파일을 다시 선택하세요.')
        with closing(sqlite3.connect(storage.readonly_uri(folder/'prepared'/'as.db'),uri=True)) as src, app.db() as dst:
            dst.execute('BEGIN IMMEDIATE')
            if fingerprint(dst) != info['stamp']:
                raise RestoreConflict('백업 선택 후 현재 자료가 변경되었습니다. 다른 PC의 작업을 멈추고 백업 파일을 다시 선택하세요.')
            # A consistent independent rollback backup is required before any deletion.
            recovery = storage.snapshot(app.DATA/'as.db',Path(os.environ.get('AS_BACKUP_DIR',str(app.DATA/'backups'))),'before-restore')
            old_versions = [int(json.loads(row[0]).get('version',0)) for row in dst.execute('SELECT body FROM slips')]
            new_versions = [int(json.loads(row[0]).get('version',0)) for row in src.execute('SELECT body FROM slips')]
            version = max([0,*old_versions,*new_versions])+1
            reserved = list(dst.execute("SELECT key,value FROM app_meta WHERE key LIKE 'slip_number_max:%'"))
            for table in TABLES: dst.execute(f'DELETE FROM {table}')
            for table, columns in TABLES.items():
                for row in src.execute(f'SELECT {",".join(columns)} FROM {table}'):
                    if table == 'slips':
                        record = app.normalize(dict(json.loads(row[1]),id=row[0],version=version))
                        dst.execute('INSERT INTO slips(id,body) VALUES(?,?)',(row[0],'{}'))
                        storage.save_record(dst,row[0],record)
                    else:
                        dst.execute(f'INSERT OR REPLACE INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',tuple(row))
            for key,value in reserved:
                dst.execute('INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(app_meta.value AS INTEGER),CAST(excluded.value AS INTEGER))',(key,value))
            dst.execute("INSERT OR REPLACE INTO app_meta VALUES('restore_generation',?)",(uuid.uuid4().hex,))
            for table, seq in src.execute('SELECT name,seq FROM sqlite_sequence'):
                if table in TABLES: bump_sequence(dst,table,seq)
            for table, seq in list(dst.execute('SELECT name,seq FROM sqlite_sequence')):
                if table in ('slips','suppliers','products','receipt_options'):
                    dst.execute('INSERT OR REPLACE INTO app_meta VALUES(?,?)',('restore_sequence:'+table,str(seq)))
            result = summary(dst)
        # The ordinary commit hook makes the complete restoration durable on NAS.
        shutil.rmtree(folder,ignore_errors=True)
        return {'ok':True,'restored':result,'safetyBackup':recovery.name}
