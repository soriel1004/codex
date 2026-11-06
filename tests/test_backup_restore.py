import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app
import auto_server
import backup_restore
import storage


class BackupRestore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_data, self.previous_sync = app.DATA, storage.active_sync
        app.DATA = Path(self.temp.name)/'current'
        storage.active_sync = None
        self.coordinators = []
        self.server = app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        for c in reversed(self.coordinators): c.close(clean=False)
        app.DATA, storage.active_sync = self.previous_data, self.previous_sync
        self.temp.cleanup()

    def request(self, path, data=None, binary=None):
        body = binary if binary is not None else json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.base+path,data=body,headers={'Content-Type':'application/octet-stream' if binary is not None else 'application/json'})
        with urllib.request.urlopen(request,timeout=10) as response:
            content = response.read()
            return json.loads(content) if response.headers.get_content_type() == 'application/json' else content

    def error(self, status, path, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as raised: self.request(path,**kwargs)
        self.assertEqual(raised.exception.code,status,raised.exception.read())

    def create(self, name='우리 부품'):
        if not self.request('/api/suppliers'): self.request('/api/suppliers',{'name':'받는 업체','phone':'02-1111-2222'})
        return self.request('/api/slips',{'date':'2026-10-19','supplier':'받는 업체','phone':'02-1111-2222','company':'보낸 업체','companyPhone':'010-1111-2222','kind':'AS 발송','status':'발송',
            'requesters':['GN'],'courier':'CJ대한통운','trackingNumber':'0000123456789','note':'전표 비고',
            'items':[{'name':name,'serial':'000OUT','qrCodeNumber':'000QR','productionMonth':'2025-09','note':'품목 비고','fault':'부팅 안됨','qty':2}]})

    def restore(self, content):
        preview = self.request('/api/restore/preview',binary=content)
        return self.request('/api/restore/commit',{'token':preview['token'],'confirmed':True})

    def activate(self, pc):
        c = auto_server.SharedServer(Path(self.temp.name)/'NAS',Path(self.temp.name)/pc)
        self.coordinators.append(c)
        self.assertTrue(c.claim()); c.prepare(); c.activate()
        return c

    def test_roundtrip_every_record_catalog_setting_safety_backup_and_stale_edit_fence(self):
        self.request('/api/settings',{'company':'원래 보낸 업체','companyPhone':'010-9999-0000','manager':'담당자'})
        slip = self.create()
        slip = self.request('/api/returns',{'id':slip['id'],'version':slip['version'],'date':'2026-10-20','result':'교환','mbRepairReport':'수리내역 보존','items':[{'index':0,'qty':1,'receivedSerial':'000IN','repairContent':'품목별 수리 보존'}]})
        self.request('/api/products',{'name':'등록 품명'})
        self.request('/api/receipt-options',{'name':'사용자 입고처리'})
        content = self.request('/api/backup')
        slip['note'] = '이후 변경된 자료'
        stale = self.request('/api/slips',slip)
        later = self.create('이후 추가 품목')
        self.request('/api/settings',{'company':'변경된 업체'})
        preview = self.request('/api/restore/preview',binary=content)
        self.assertEqual(preview['backup']['slips'],1)
        self.assertEqual(preview['current']['slips'],2)
        restored = self.request('/api/restore/commit',{'token':preview['token'],'confirmed':True})
        self.assertTrue(restored['ok'])
        records = self.request('/api/slips'); self.assertEqual(len(records),1)
        current = records[0]
        self.assertEqual(current['note'],'전표 비고')
        self.assertEqual(current['items'][0]['note'],'품목 비고')
        self.assertEqual(current['items'][0]['serial'],'000OUT')
        self.assertEqual(current['items'][0]['receivedSerial'],'000IN')
        self.assertEqual(current['items'][0]['qrCodeNumber'],'000QR')
        self.assertEqual(current['items'][0]['mbRepairReport'],'수리내역 보존')
        self.assertEqual(current['items'][0]['repairContent'],'품목별 수리 보존')
        self.assertEqual(current['returns'],slip['returns'])
        self.assertEqual(self.request('/api/settings')['company'],'원래 보낸 업체')
        self.assertIn('사용자 입고처리',self.request('/api/receipt-options'))
        self.assertIn('등록 품명',[p['name'] for p in self.request('/api/products')])
        self.error(409,'/api/slips',data=stale)
        self.error(409,'/api/slips/delete',data={'id':stale['id'],'version':stale['version']})
        safety = self.request('/api/restore/safety?name='+restored['safetyBackup'])
        safety_path = Path(self.temp.name)/'safety.db'; safety_path.write_bytes(safety)
        with closing(sqlite3.connect(safety_path)) as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM slips').fetchone()[0],2)
        new = self.create('복원 후 추가')
        self.assertGreater(new['id'],later['id'])
        self.assertEqual(new['number'],'261019-03')
        with zipfile.ZipFile(io.BytesIO(self.request('/api/export'))) as report: self.assertIsNone(report.testzip())

    def test_invalid_files_preview_cancel_and_changed_data_never_replace_current_data(self):
        self.create()
        with app.db() as c: before = backup_restore.fingerprint(c)
        self.error(400,'/api/restore/preview',binary=b'not a database file at all')
        unrelated = Path(self.temp.name)/'other.db'
        with closing(sqlite3.connect(unrelated)) as c: c.execute('CREATE TABLE unrelated (x)'); c.commit()
        self.error(400,'/api/restore/preview',binary=unrelated.read_bytes())
        content = self.request('/api/backup')
        preview = self.request('/api/restore/preview',binary=content)
        with app.db() as c: self.assertEqual(backup_restore.fingerprint(c),before)
        self.error(400,'/api/restore/commit',data={'token':preview['token']})
        self.create('백업 확인 중 새 전표')
        self.error(409,'/api/restore/commit',data={'token':preview['token'],'confirmed':True})
        self.assertEqual(len(self.request('/api/slips')),2)
        self.assertEqual(list((app.DATA/'backups').glob('before-restore-*')),[])

    def test_legacy_backup_and_intentionally_empty_catalogs(self):
        legacy = Path(self.temp.name)/'legacy.db'
        with closing(sqlite3.connect(legacy)) as c:
            c.execute('CREATE TABLE slips(id INTEGER PRIMARY KEY,body TEXT)')
            c.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,body TEXT)')
            c.execute('INSERT INTO settings VALUES(1,?)',(json.dumps({'company':'예전 보낸이'}),))
            c.execute('INSERT INTO slips VALUES(1,?)',(json.dumps({'number':'261019-01','date':'2026-10-19','supplier':'받는 업체','company':'예전 보낸이','kind':'AS 발송','status':'발송','items':[{'name':'메모리','model':'DDR5','qty':1}]}),))
            c.commit()
        self.restore(legacy.read_bytes())
        self.assertEqual(self.request('/api/slips')[0]['items'][0]['name'],'메모리 DDR5')
        self.assertEqual(self.request('/api/settings')['company'],'예전 보낸이')
        self.request('/api/suppliers'); self.request('/api/products'); self.request('/api/receipt-options')
        with app.db() as c:
            for table in ('suppliers','products','receipt_options'): c.execute('DELETE FROM '+table)
        content = self.request('/api/backup')
        self.request('/api/suppliers',{'name':'나중에 추가한 업체'})
        self.restore(content)
        self.assertEqual(self.request('/api/suppliers'),[])
        self.assertEqual(self.request('/api/products'),[])
        self.assertEqual(self.request('/api/receipt-options'),[])

    def test_uploaded_triggers_are_not_installed_and_safety_backup_failure_prevents_restore(self):
        self.request('/api/settings',{'company':'보존할 설정'})
        self.create()
        source = Path(self.temp.name)/'with-trigger.db'
        source.write_bytes(self.request('/api/backup'))
        with closing(sqlite3.connect(source)) as c:
            c.execute('CREATE TRIGGER unwanted AFTER INSERT ON slips BEGIN DELETE FROM settings; END')
            c.commit()
        content = source.read_bytes()
        preview = self.request('/api/restore/preview',binary=content)
        with patch.object(storage,'snapshot',side_effect=OSError('백업 공간이 없습니다.')):
            self.error(503,'/api/restore/commit',data={'token':preview['token'],'confirmed':True})
        self.assertEqual(len(self.request('/api/slips')),1)
        self.restore(content)
        self.create('복원 후 등록')
        self.assertEqual(self.request('/api/settings')['company'],'보존할 설정')
        with app.db() as c: self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='unwanted'").fetchone())
        self.error(400,'/api/restore/safety?name=../as.db')

    def test_nas_failure_rolls_back_and_completed_restore_survives_server_handoff(self):
        first = self.activate('PC1')
        slip = self.create('백업 원본')
        with app.db() as c: c.execute("UPDATE sqlite_sequence SET seq=90 WHERE name='slips'")
        content = self.request('/api/backup')
        slip['note'] = '현재 자료'
        self.request('/api/slips',slip)
        preview = self.request('/api/restore/preview',binary=content)
        original = auto_server.atomic_json
        def disconnected(path, *args, **kwargs):
            if path.parent.name == 'transactions': raise OSError('NAS unavailable')
            return original(path,*args,**kwargs)
        with patch.object(auto_server,'atomic_json',side_effect=disconnected):
            self.error(503,'/api/restore/commit',data={'token':preview['token'],'confirmed':True})
        self.assertEqual(self.request('/api/slips')[0]['note'],'현재 자료')
        self.restore(content)
        self.assertEqual(self.request('/api/slips')[0]['note'],'전표 비고')
        first.close(clean=False)
        self.activate('PC2')
        self.assertEqual(self.request('/api/slips')[0]['note'],'전표 비고')
        self.assertEqual(self.create('인계 후 추가')['id'],91)


if __name__ == '__main__': unittest.main()
