import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from unittest.mock import patch
import app
import launcher
import storage


class ServerStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = app.DATA
        app.DATA = Path(self.tmp.name) / 'data'

    def tearDown(self):
        app.DATA = self.previous
        self.tmp.cleanup()

    def slip(self, number='261019-01', **changes):
        record = dict(number=number,date='2026-10-19',supplier='받는 업체',company='보낸 업체',phone='02-123-4567',
                      kind='AS 발송',status='발송',version=0,requesters=['GN'],trackingNumber='0000-123 456789',courier='CJ대한통운',
                      note='점검 요청',items=[dict(name='DDR5 메모리',serial='AB-001 234',qty=2,fault='부팅 불가'),
                                             dict(name='후속 GPU',serial='SECOND-007',qrCodeNumber='000QR-CODE',qty=1)])
        record.update(changes)
        return app.normalize(record)

    def seed(self, records):
        with app.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT OR IGNORE INTO suppliers(name,phone,address) VALUES('받는 업체','','')")
            for record in records:
                slip_id = c.execute("INSERT INTO slips(body) VALUES('{}')").lastrowid
                storage.save_record(c,slip_id,record)

    def page(self, **params):
        with app.db() as c:
            return storage.page_records(c,{key:[str(value)] for key,value in params.items()},app.normalize)

    def test_search_and_filters_across_all_items_and_receipts(self):
        completed = self.slip('261019-02',requesters=[],trackingNumber='9988',returns=[
            dict(date='2026-10-21',result='교환 입고',mbRepairReport='전원부 교체',items=[
                dict(index=0,qty=2,receivedSerial='NEW-IN-0099\nEXTRA-IN-0010'),dict(index=1,qty=1)])])
        self.seed([self.slip(),completed])
        cases = [('outboundSerial','ａｂ００１２３４',2),('name','후속 gpu',2),('qrCodeNumber','000qrcode',2),
                 ('receivedSerial','newin0099',1),('receivedSerial','extrain0010',1),('trackingNumber','0000123456789',1),
                 ('all','받는업체 gpu second007',2),('details','전원부 교체',1),('number','26101902',1),
                 ('all','021234567',2),('all','완료',1),('all','trackingNumber',0),('all','999999999',0),
                 ('serial','0099extrain',0),('all','---',0),('all','%_',0),('all','" OR *',0)]
        for field, query, expected in cases:
            with self.subTest(field=field,q=query):
                self.assertEqual(self.page(view='all',field=field,q=query)['total'],expected)
        self.assertEqual(self.page(view='active',q='newin0099')['total'],0)
        self.assertEqual(self.page(view='completed',q='newin0099')['total'],1)
        self.assertEqual(self.page(view='all',requester='GN')['total'],1)
        self.assertEqual(self.page(view='all',requester='none')['total'],1)
        self.assertEqual(self.page(view='all',status='반환완료')['total'],1)

    def test_pagination_normalises_only_requested_rows_and_indexes_number_lookups(self):
        self.seed(self.slip(f'261019-{i:05d}') for i in range(1,122))
        with app.db() as c:
            with patch.object(app,'normalize',wraps=app.normalize) as normalise:
                result = storage.page_records(c,{'view':['all'],'page':['2'],'size':['25']},app.normalize)
            self.assertEqual(normalise.call_count,25)
            self.assertEqual(result['total'],121)
            self.assertEqual(result['rows'][0]['number'],'261019-00096')
            self.assertEqual(result['stats'],dict(total=121,active=121,completed=0,suppliers=1))
            plan = str(c.execute('EXPLAIN QUERY PLAN SELECT 1 FROM slips WHERE number=?',('261019-00002',)).fetchall())
            self.assertIn('slips_number_idx',plan)
            c.commit()
            traced=[]; c.set_trace_callback(traced.append)
            c.execute('BEGIN IMMEDIATE')
            self.assertEqual(app.next_slip_number(c,'2026-10-19'),'261019-122')
            self.assertFalse(any('SELECT body FROM slips' in sql for sql in traced))
        self.assertEqual(self.page(view='all',page=999,size=25)['page'],5)
        self.assertEqual(len(self.page(view='all',page=999,size=25)['rows']),21)
        for params in ({'size':1000},{'page':'x'},{'field':'injected'},{'q':'a'*501}):
            with self.assertRaises(ValueError): self.page(**params)

    def test_legacy_migration_is_backed_up_and_preserves_details_and_reservations(self):
        app.DATA.mkdir()
        legacy = self.slip()
        legacy.pop('returns')
        legacy['status']='반환완료'; legacy['returned']='2026-10-21'
        with sqlite3.connect(app.DATA/'as.db') as c:
            c.execute('CREATE TABLE slips(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)')
            c.execute('INSERT INTO slips(body) VALUES(?)',(json.dumps(legacy),))
            c.execute('CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO app_meta VALUES('slip_number_max:261019','99')")
        with app.db() as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],storage.SCHEMA_VERSION)
            c.execute('BEGIN IMMEDIATE')
            self.assertEqual(app.next_slip_number(c,'2026-10-19'),'261019-100')
        migrated = self.page(view='completed')['rows'][0]
        self.assertEqual(migrated['returned'],'2026-10-21')
        self.assertEqual(migrated['items'][0]['serial'],'AB-001 234')
        self.assertEqual(len(list((app.DATA/'backups').glob('before-V2-migration-*.db'))),1)
        with app.db() as c:
            raw=json.loads(c.execute('SELECT body FROM slips').fetchone()[0])
            self.assertNotIn('elapsedDays',raw)
            self.assertNotIn('returnedQty',raw['items'][0])

    def test_search_changes_are_transactional_and_deleted_rows_are_removed(self):
        self.seed([self.slip()])
        with app.db() as c:
            slip_id=c.execute('SELECT id FROM slips').fetchone()[0]
            changed=self.slip(); changed['items'][0]['serial']='CHANGED-003'
            storage.save_record(c,slip_id,changed)
        self.assertEqual(self.page(view='all',q='ab001234')['total'],0)
        self.assertEqual(self.page(view='all',q='changed003')['total'],1)
        with app.db() as c: c.execute('DELETE FROM slips WHERE id=?',(slip_id,))
        with app.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM slip_search').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM slip_search_rows').fetchone()[0],0)

    def test_snapshot_restores_while_server_database_is_open_and_never_overwrites_import(self):
        self.seed([self.slip()])
        with app.db() as c:
            target=storage.snapshot(app.DATA/'as.db',Path(self.tmp.name)/'NAS-backups')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM slips').fetchone()[0],1)
        with sqlite3.connect(target) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM slips').fetchone()[0],1)
        restored=Path(self.tmp.name)/'restored'/'as.db'
        launcher.import_legacy(target,restored)
        with self.assertRaises(ValueError): launcher.import_legacy(target,restored)
        self.assertEqual(len(list((Path(self.tmp.name)/'NAS-backups').glob('*.db'))),1)

    def test_single_server_lock_network_guard_and_client_does_not_open_database(self):
        with storage.ServerLock(app.DATA):
            with self.assertRaises(ValueError):
                with storage.ServerLock(app.DATA): pass
        with storage.ServerLock(app.DATA): pass
        with self.assertRaises(ValueError): storage.require_local(r'\\NAS\share\data')
        self.assertEqual(storage.readonly_uri(PureWindowsPath(r'\\NAS\shared folder\as.db')),'file:////NAS/shared%20folder/as.db?mode=ro')
        with patch.object(launcher,'read_config',return_value={'server_url':'http://server:8000'}), \
             patch.object(app,'open_program_window') as opened, patch.object(app,'db') as database:
            launcher.main()
            opened.assert_called_once_with('http://server:8000')
            database.assert_not_called()
        with self.assertRaises(ValueError): launcher.server_data({'server_pc':'definitely-not-this-PC','data_dir':str(app.DATA)})

    def test_old_sqlite_search_fallback_and_offline_compaction_preserve_records(self):
        app.DATA.mkdir()
        with sqlite3.connect(app.DATA/'as.db') as c:
            c.execute('CREATE TABLE slip_search(value TEXT,compact TEXT,slip_id INTEGER,field TEXT)')
        self.seed([self.slip()])
        with app.db() as c: self.assertFalse(c.search_fts)
        self.assertEqual(self.page(view='all',field='serial',q='ab001234')['total'],1)
        before=app.records()
        with storage.ServerLock(app.DATA):
            with self.assertRaises(ValueError): launcher.compact_database(app.DATA)
        launcher.compact_database(app.DATA)
        self.assertEqual(app.records(),before)
        self.assertEqual(len(list((app.DATA/'backups').glob('before-compact-*.db'))),1)

    def test_server_setup_imports_without_changing_nas_source_and_reuses_custom_paths(self):
        shared=Path(self.tmp.name)/'shared'; shared.mkdir()
        source=shared/'old.db'
        with sqlite3.connect(source) as c:
            c.execute('CREATE TABLE slips(id INTEGER PRIMARY KEY,body TEXT NOT NULL)')
            c.execute('INSERT INTO slips VALUES(1,?)',(json.dumps(self.slip()),))
        source_bytes=source.read_bytes()
        local=Path(self.tmp.name)/'custom-local-data'; backup=shared/'NAS-backups'
        with patch.object(launcher,'ROOT',shared), patch.object(launcher,'CONFIG',shared/'server-config.json'), \
             patch('builtins.input',side_effect=['Y',str(local),'http://192.168.0.10:8123',str(backup),str(source)]), \
             redirect_stdout(io.StringIO()):
            launcher.setup()
        self.assertEqual(source.read_bytes(),source_bytes)
        self.assertEqual(len(app.records()),1)
        self.assertIn('http://192.168.0.10:8123',(shared/'AS-client.url').read_text())
        with patch.object(launcher,'ROOT',shared), patch.object(launcher,'CONFIG',shared/'server-config.json'), \
             patch('builtins.input',side_effect=['Y','','','']), redirect_stdout(io.StringIO()):
            launcher.setup()
            config=launcher.read_config()
        self.assertEqual(Path(config['data_dir']),local)
        self.assertEqual(config['port'],8123)
        self.assertEqual(Path(config['backup_dir']),backup)
        self.assertEqual(source.read_bytes(),source_bytes)

    def test_two_http_clients_saving_and_reading_same_server(self):
        self.seed([])
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,data=None,headers=None):
            req=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
            try:
                with urllib.request.urlopen(req) as response: return response.status,json.load(response)
            except urllib.error.HTTPError as error: return error.code,json.load(error)
        try:
            self.assertEqual(request('/api/settings',{'company':'invalid'},{'Origin':'http://other-site.example'})[0],403)
            self.assertEqual(request('/api/settings',{'company':'invalid'},{'Content-Type':'text/plain'})[0],415)
            self.assertEqual(request('/api/settings',{'company':'same origin'},{'Origin':base})[0],200)
            with ThreadPoolExecutor(max_workers=8) as clients:
                created=list(clients.map(lambda _:request('/api/slips',self.slip()),range(16)))
            self.assertTrue(all(code==200 for code,_ in created))
            self.assertEqual(len({row['number'] for _,row in created}),16)
            original=created[0][1]
            one,two=copy.deepcopy(original),copy.deepcopy(original)
            one['note']='PC 1 수정'; two['note']='PC 2 수정'
            with ThreadPoolExecutor(max_workers=2) as clients:
                results=list(clients.map(lambda row:request('/api/slips',row),[one,two]))
            self.assertEqual(sorted(code for code,_ in results),[200,409])
            updated=request('/api/slips/'+str(original['id']))[1]
            self.assertEqual(updated['version'],1)
            self.assertEqual(request('/api/slips/page?view=all&size=25')[1]['total'],16)
            with urllib.request.urlopen(base+'/api/backup') as response:
                path=Path(self.tmp.name)/'download.db'; path.write_bytes(response.read())
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM slips').fetchone()[0],16)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
