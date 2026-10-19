import importlib.util, io, json, os, tempfile, threading, unittest, urllib.request, urllib.error, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();os.environ['AS_DATA_DIR']=cls.tmp.name
        spec=importlib.util.spec_from_file_location('app',Path(__file__).resolve().parents[1]/'app.py');cls.app=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.app)
        cls.server=cls.app.ThreadingHTTPServer(('127.0.0.1',0),cls.app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.tmp.cleanup()
    def req(self,path,data=None):
        request=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as r:return r.read()
    def test_full_workflow(self):
        self.assertIn('AS'.encode(),self.req('/'))
        self.req('/api/settings',{'company':'테스트 업체'})
        self.assertEqual(json.loads(self.req('/api/settings'))['company'],'테스트 업체')
        data={'date':'2026-10-19','supplier':'공급사 A','company':'테스트 업체','kind':'AS 발송','status':'발송','items':[{'name':'메모리','model':'DDR5','serial':'001234','qty':2,'fault':'부팅 불가'}]}
        saved=json.loads(self.req('/api/slips',data));self.assertTrue(saved['number'].startswith('AS-20261019-'))
        saved.update(status='반환완료',returned='2026-10-20',result='교환')
        updated=json.loads(self.req('/api/slips',saved));self.assertEqual(updated['number'],saved['number'])
        rows=json.loads(self.req('/api/slips'));self.assertEqual(len(rows),1);self.assertEqual(rows[0]['result'],'교환')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            for n in z.namelist():ET.fromstring(z.read(n))
            sheet=z.read('xl/worksheets/sheet1.xml').decode();self.assertIn('001234',sheet);self.assertIn('교환',sheet)
        # Reopening the database preserves the record; this is not browser-only state.
        with self.app.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM slips').fetchone()[0],1)
    def test_invalid_record_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.req('/api/slips',{'supplier':'','date':'2026-10-19'})
        self.assertEqual(error.exception.code,400)
    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.req('/../app.py')
        self.assertEqual(error.exception.code,404)
