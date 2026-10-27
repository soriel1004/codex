import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch
import app
import auto_server
import launcher
import storage


class SharedCommits(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'NAS'; self.root.mkdir()
        self.previous=app.DATA
        self.coordinators=[]

    def tearDown(self):
        for coordinator in reversed(self.coordinators): coordinator.close(clean=False)
        storage.active_sync=None
        app.DATA=self.previous
        self.temp.cleanup()

    def start(self, pc):
        coordinator=auto_server.SharedServer(self.root,Path(self.temp.name)/pc)
        self.coordinators.append(coordinator)
        self.assertTrue(coordinator.claim())
        coordinator.prepare(); coordinator.activate()
        return coordinator

    def write(self, text):
        with app.db() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps({'company':text}),))

    def company(self):
        with app.db() as c:
            row=c.execute('SELECT body FROM settings').fetchone()
            return json.loads(row[0])['company'] if row else None

    def test_durable_journal_handoff_and_single_owner(self):
        first=self.start('PC1'); self.write('최신 내용')
        second=auto_server.SharedServer(self.root,Path(self.temp.name)/'PC2')
        self.assertFalse(second.claim())
        self.assertFalse((Path(self.temp.name)/'PC2').exists())
        first.close(clean=False)
        successor=self.start('PC2')
        self.assertEqual(self.company(),'최신 내용')
        self.assertGreater(successor.generation,first.generation)
        self.write('PC2에서 수정')
        successor.close(clean=False)
        self.start('PC3')
        self.assertEqual(self.company(),'PC2에서 수정')

    def test_nas_write_failure_rolls_back_local_transaction(self):
        first=self.start('PC1'); self.write('원본')
        original=auto_server.atomic_json
        def fail(path,*args,**kwargs):
            if path.parent.name=='transactions': raise OSError('NAS unavailable')
            return original(path,*args,**kwargs)
        with patch.object(auto_server,'atomic_json',side_effect=fail):
            with self.assertRaises(storage.SharedStoreError): self.write('실패한 내용')
        self.assertEqual(self.company(),'원본')
        self.assertEqual(first.sequence,1)
        self.write('재연결 후 저장')
        first.close(clean=False)
        self.start('PC2')
        self.assertEqual(self.company(),'재연결 후 저장')

    def test_crash_after_durable_publication_recovers_even_without_local_commit(self):
        first=self.start('PC1')
        c=app.db()
        try:
            c.execute('INSERT INTO settings VALUES(1,?)',(json.dumps({'company':'NAS에 기록된 내용'}),))
            def interrupted(): raise sqlite3.OperationalError('process stopped before local commit')
            with self.assertRaises(storage.SharedStoreError): first.commit(c,interrupted)
            c.rollback()
        finally: c.close()
        self.assertTrue(first.failed)
        first.close(clean=False)
        self.start('PC2')
        self.assertEqual(self.company(),'NAS에 기록된 내용')

    def test_new_generation_fences_old_server_and_immutable_files_do_not_overwrite(self):
        first=self.start('PC1'); self.write('원본')
        owner=first.owner('starting'); owner.update(token=uuid.uuid4().hex,generation=first.generation+1)
        auto_server.atomic_json(first.shared/'owners'/f'{owner["generation"]:020d}.json',owner,exclusive=True)
        with self.assertRaises(storage.SharedStoreError): self.write('이전 서버의 쓰기')
        self.assertEqual(self.company(),'원본')
        path=first.shared/'transactions'/'00000000000000001-test.json'
        auto_server.atomic_json(path,{'value':1},exclusive=True)
        with self.assertRaises(FileExistsError): auto_server.atomic_json(path,{'value':2},exclusive=True)
        self.assertEqual(auto_server.read_json(path),{'value':1})

    def test_missing_tail_or_corrupted_record_never_publishes_an_older_checkpoint(self):
        first=self.start('PC1'); self.write('저장 완료')
        transaction=first.shared/'transactions'/'00000000000000000001.json'
        transaction.unlink()
        manifests_before={path.name for path in (first.shared/'manifests').glob('*.json')}
        first.close(clean=False)
        second=auto_server.SharedServer(self.root,Path(self.temp.name)/'PC2')
        self.coordinators.append(second); self.assertTrue(second.claim())
        with self.assertRaisesRegex(storage.SharedStoreError,'누락'): second.prepare()
        second.close(clean=False)
        self.assertEqual({path.name for path in (first.shared/'manifests').glob('*.json')},manifests_before)

    def test_checkpoint_rotation_and_replay_preserve_latest_data(self):
        first=self.start('PC1')
        for i in range(10):
            self.write('내용 '+str(i))
            if i in (4,9): first.checkpoint()
        self.assertEqual(len(list((first.shared/'manifests').glob('*.json'))),2)
        self.assertEqual(len(list((first.shared/'transactions').glob('*.json'))),5)
        first.close(clean=False)
        self.start('PC2')
        self.assertEqual(self.company(),'내용 9')

    def test_waiting_starter_can_become_server_after_previous_one_closes(self):
        first=self.start('PC1')
        second=auto_server.SharedServer(self.root,Path(self.temp.name)/'PC2')
        self.coordinators.append(second)
        self.assertFalse(second.claim())
        timer=threading.Timer(.1,lambda:first.close(clean=False)); timer.start()
        try:
            self.assertIsNone(auto_server.client_url(self.root,timeout=3,try_claim=second.claim))
            second.prepare(); second.activate()
        finally: timer.join()

    def test_corrupted_payload_is_rejected_without_replacing_shared_data(self):
        first=self.start('PC1'); self.write('원본')
        path=first.shared/'transactions'/'00000000000000000001.json'
        envelope=auto_server.read_json(path)
        envelope['payload']['changes'][0]['row']['body']='changed without checksum'
        auto_server.atomic_json(path,envelope)
        first.close(clean=False)
        second=auto_server.SharedServer(self.root,Path(self.temp.name)/'PC2')
        self.coordinators.append(second); self.assertTrue(second.claim())
        with self.assertRaisesRegex(storage.SharedStoreError,'손상'): second.prepare()
        self.assertEqual(len(list((first.shared/'manifests').glob('*.json'))),1)

    def test_uncertain_publish_is_fenced_and_recovered_without_duplicate_retry(self):
        first=self.start('PC1')
        original=auto_server.atomic_json
        def lost_reply(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path.parent.name=='transactions': raise OSError('reply lost after publication')
            return result
        with patch.object(auto_server,'atomic_json',side_effect=lost_reply):
            with self.assertRaisesRegex(storage.SharedStoreError,'결과를 확정'): self.write('이미 NAS에 게시됨')
        self.assertTrue(first.failed)
        first.close(clean=False)
        self.start('PC2')
        self.assertEqual(self.company(),'이미 NAS에 게시됨')

    def test_v2_migration_uses_latest_server_data_and_rejects_wrong_pc(self):
        self.start('source'); self.write('V2 로컬 최신 자료')
        storage.active_sync=None
        source=app.DATA
        self.coordinators[-1].close(clean=False)
        config={'server_pc':socket.gethostname(),'server_url':'http://127.0.0.1:8000','port':8000,'data_dir':str(source),'backup_dir':str(self.root/'backups')}
        target=Path(self.temp.name)/'migration'/'as.db'
        def inspect(root, importer):
            importer(target)
        with patch.object(launcher,'ROOT',self.root/'another-share'),patch.object(launcher,'read_config',return_value=config), \
             patch.object(sys,'argv',['launcher.py','--auto']),patch.object(auto_server,'run',side_effect=inspect):
            launcher.main()
        with sqlite3.connect(target) as c:
            self.assertEqual(json.loads(c.execute('SELECT body FROM settings').fetchone()[0])['company'],'V2 로컬 최신 자료')
        target.unlink()
        config['server_pc']='not-this-PC'
        with patch.object(launcher,'ROOT',self.root/'another-share'),patch.object(launcher,'read_config',return_value=config), \
             patch.object(sys,'argv',['launcher.py','--auto']),patch.object(auto_server,'run',side_effect=inspect):
            with self.assertRaises(ValueError): launcher.main()
        self.assertFalse(target.exists())


class AutoProcesses(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'NAS'; self.root.mkdir()
        self.processes=[]; self.logs=[]

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None: process.kill()
            process.wait(timeout=10)
        for log in self.logs: log.close()
        self.temp.cleanup()

    def launch(self, pc):
        log=(Path(self.temp.name)/(pc+'-'+uuid.uuid4().hex+'.log')).open('w+')
        self.logs.append(log)
        process=subprocess.Popen([sys.executable,'-u','-c',
            "from pathlib import Path; import auto_server,sys; print('ROLE:',auto_server.run(Path(sys.argv[1]),local_base=Path(sys.argv[2]),open_browser=False),flush=True)",
            str(self.root),str(Path(self.temp.name)/pc)],cwd=Path(__file__).resolve().parents[1],
            env={**os.environ,'AS_AUTO_PORT':'0','AS_ADVERTISE_HOST':'127.0.0.1'},stdout=log,stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process,log

    def ready(self, after=0):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                owner=auto_server.read_owner(self.root/'.as-shared')
                if owner['generation']>after and owner['state']=='ready':
                    with urllib.request.urlopen(owner['url']+'/api/health',timeout=1) as response:
                        health=json.load(response)
                    if health.get('leader_token')==owner['token']: return owner
            except (OSError,ValueError): pass
            time.sleep(.05)
        for log in self.logs:
            log.flush(); log.seek(0); print(log.read())
        self.fail('Automatic server did not become ready')

    def request(self, owner, path, data=None):
        request=urllib.request.Request(owner['url']+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=10) as response: return json.load(response)
        except Exception:
            for log in self.logs:
                log.flush(); log.seek(0); print(log.read())
            raise

    def test_three_simultaneous_launches_have_one_server_and_two_clients(self):
        processes=[self.launch(pc) for pc in ('PC1','PC2','PC3')]
        owner=self.ready()
        deadline=time.monotonic()+10
        while sum(process.poll() is None for process,_ in processes)>1 and time.monotonic()<deadline: time.sleep(.05)
        self.assertEqual(sum(process.poll() is None for process,_ in processes),1)
        for process,log in processes:
            if process.poll() is not None:
                self.assertEqual(process.returncode,0)
                log.flush(); log.seek(0); self.assertIn('ROLE: client',log.read())
        self.assertEqual(self.request(owner,'/api/slips/page?view=all')['total'],0)

    def test_pc1_crash_pc2_handoff_and_pc3_handoff_preserve_slips_returns_and_numbers(self):
        pc1,_=self.launch('PC1'); first=self.ready()
        self.request(first,'/api/suppliers',{'name':'자동 접속 업체','phone':'02-123-4567'})
        self.request(first,'/api/settings',{'company':'우리 업체'})
        slip=self.request(first,'/api/slips',{'date':'2026-10-19','supplier':'자동 접속 업체','company':'우리 업체','status':'발송','kind':'AS 발송',
            'requesters':['GN'],'trackingNumber':'0000123456789','items':[{'name':'메인보드','serial':'000OUT','qrCodeNumber':'000QR','qty':2}]})
        slip=self.request(first,'/api/returns',{'id':slip['id'],'version':slip['version'],'date':'2026-10-20','items':[{'index':0,'qty':1,'receivedSerial':'000IN'}],'mbRepairReport':'수리 내역'})
        follower,log=self.launch('PC2'); follower.wait(timeout=10)
        self.assertEqual(follower.returncode,0)
        self.assertFalse((Path(self.temp.name)/'PC2').exists())
        pc1.kill(); pc1.wait(timeout=10)
        pc2,_=self.launch('PC2'); second=self.ready(first['generation'])
        restored=self.request(second,'/api/slips/'+str(slip['id']))
        self.assertEqual(restored['items'][0]['receivedSerial'],'000IN')
        self.assertEqual(restored['items'][0]['qrCodeNumber'],'000QR')
        self.assertEqual(restored['items'][0]['mbRepairReport'],'수리 내역')
        restored['note']='PC2에서 수정'
        saved=self.request(second,'/api/slips',restored)
        pc2.kill(); pc2.wait(timeout=10)
        self.launch('PC3'); third=self.ready(second['generation'])
        final=self.request(third,'/api/slips/'+str(slip['id']))
        self.assertEqual(final['note'],'PC2에서 수정')
        self.assertEqual(final['version'],saved['version'])
        self.assertEqual(final['items'][0]['returnedQty'],1)
        new=self.request(third,'/api/slips',{'date':'2026-10-19','supplier':'자동 접속 업체','company':'우리 업체','status':'발송','kind':'AS 발송','items':[{'name':'추가 부품','qty':1}]})
        self.assertEqual(new['number'],'261019-02')
        self.assertEqual(self.request(third,'/api/settings')['company'],'우리 업체')


if __name__=='__main__': unittest.main()
