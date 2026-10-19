import copy
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous_data = os.environ.get('AS_DATA_DIR')
        os.environ['AS_DATA_DIR'] = cls.tmp.name
        spec = importlib.util.spec_from_file_location('app', Path(__file__).resolve().parents[1] / 'app.py')
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.server = cls.app.ThreadingHTTPServer(('127.0.0.1', 0), cls.app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()
        if cls.previous_data is None:
            os.environ.pop('AS_DATA_DIR', None)
        else:
            os.environ['AS_DATA_DIR'] = cls.previous_data

    def setUp(self):
        with self.app.db() as c:
            for table in ('slips', 'settings', 'suppliers', 'products', 'app_meta'):
                c.execute('DELETE FROM ' + table)
        self.post('/api/suppliers', {'name': '공급사 A', 'phone': '02-123-4567'})
        for name in ('메모리 DDR5', '그래픽카드', '메인보드'):
            self.post('/api/products', {'name': name})

    def req(self, path, data=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as r:
            return r.read()

    def post(self, path, data):
        return json.loads(self.req(path, data))

    def get(self, path):
        return json.loads(self.req(path))

    def create(self):
        return self.post('/api/slips', {
            'date': '2026-10-19', 'supplier': '공급사 A', 'company': '테스트 업체', 'kind': 'AS 발송', 'status': '발송',
            'items': [
                {'name': '메모리 DDR5', 'serial': '001234', 'qty': 2, 'fault': '부팅 불가'},
                {'name': '그래픽카드', 'qty': 1}, {'name': '메인보드', 'qty': 1}
            ]})

    def ret(self, s, **kwargs):
        return self.post('/api/returns', {'id': s['id'], 'version': s['version'], 'date': '2026-10-20', **kwargs})

    def assert_error(self, code, path, data):
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.req(path, data)
        self.assertEqual(err.exception.code, code)

    def test_partial_return_then_all_remaining(self):
        s = self.create()
        s = self.ret(s, items=[{'index': 0, 'qty': 1}, {'index': 2, 'qty': 1}], result='교환')
        self.assertEqual(s['status'], '부분반환')
        self.assertEqual([i['remainingQty'] for i in s['items']], [1, 1, 0])
        s = self.ret(s, all=True, date='2026-10-21')
        self.assertEqual(s['status'], '반환완료')
        self.assertEqual(s['returned'], '2026-10-21')
        self.assertEqual([i['remainingQty'] for i in s['items']], [0, 0, 0])
        self.assertEqual(len(s['returns']), 2)
        self.assertEqual(s['returns'][0]['date'], '2026-10-20')
        self.assertEqual(s['returns'][1]['items'], [{'index': 0, 'qty': 1}, {'index': 1, 'qty': 1}])
        self.assertEqual(self.get('/api/slips')[0]['returns'], s['returns'])

    def test_one_click_full_return(self):
        s = self.ret(self.create(), all=True)
        self.assertEqual(s['status'], '반환완료')
        self.assertEqual(sum(i['returnedQty'] for i in s['items']), 4)

    def test_invalid_returns_do_not_mutate_record(self):
        s = self.create()
        base = {'id': s['id'], 'version': s['version'], 'date': '2026-10-20'}
        for items in ([], [{'index': 0, 'qty': 3}], [{'index': 0, 'qty': 0}], [{'index': 0, 'qty': -1}],
                      [{'index': 0, 'qty': 1.5}], [{'index': 5, 'qty': 1}],
                      [{'index': 0, 'qty': 1}, {'index': 0, 'qty': 1}]):
            self.assert_error(400, '/api/returns', dict(base, items=items))
        self.assert_error(400, '/api/returns', dict(base, all=True, date='2026-10-18'))
        self.assert_error(400, '/api/returns', dict(base, all=True, date='2026-02-30'))
        self.assertEqual(self.get('/api/slips')[0]['returns'], [])

    def test_stale_or_repeated_returns_rejected(self):
        s = self.create()
        done = self.ret(s, all=True)
        self.assert_error(409, '/api/returns', {'id': s['id'], 'version': s['version'], 'date': '2026-10-20', 'all': True})
        self.assert_error(400, '/api/returns', {'id': done['id'], 'version': done['version'], 'date': '2026-10-20', 'all': True})
        self.assertEqual(len(self.get('/api/slips')[0]['returns']), 1)

    def test_concurrent_returns_do_not_double_count(self):
        s = self.create()
        data = {'id': s['id'], 'version': s['version'], 'date': '2026-10-20', 'all': True}
        def submit(_):
            try:
                self.post('/api/returns', data)
                return 200
            except urllib.error.HTTPError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, range(2)))
        self.assertEqual(sorted(outcomes), [200, 409])
        self.assertEqual(sum(i['returnedQty'] for i in self.get('/api/slips')[0]['items']), 4)

    def test_edit_preserves_return_history_and_locks_items(self):
        s = self.create()
        stale = copy.deepcopy(s)
        s = self.ret(s, items=[{'index': 1, 'qty': 1}])
        self.assert_error(409, '/api/slips', stale)
        changed = copy.deepcopy(s)
        changed['items'][1]['qty'] = 2
        self.assert_error(400, '/api/slips', changed)
        changed = copy.deepcopy(s)
        changed['items'].reverse()
        self.assert_error(400, '/api/slips', changed)
        s['note'] = '배송 메모 수정'
        s['returns'] = []  # API must not allow the client to remove history.
        s = self.post('/api/slips', s)
        self.assertEqual(len(s['returns']), 1)
        self.assertEqual(s['status'], '부분반환')
        self.assertEqual(s['note'], '배송 메모 수정')

    def test_cannot_set_completed_in_editor(self):
        s = self.create()
        s['status'] = '반환완료'
        self.assert_error(400, '/api/slips', s)

    def test_legacy_completed_and_open_slips(self):
        old = self.create()
        for k in ('returns', 'version'):
            old.pop(k)
        old.update(status='반환완료', returned='2026-10-20', result='구버전 수리')
        with self.app.db() as c:
            c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(old), old['id']))
        migrated = self.get('/api/slips')[0]
        self.assertEqual(migrated['status'], '반환완료')
        self.assertEqual(migrated['returns'][0]['date'], '2026-10-20')
        self.assertEqual(sum(i['returnedQty'] for i in migrated['items']), 4)
        old.update(status='발송', returned='')
        with self.app.db() as c:
            c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(old), old['id']))
        self.assertEqual(self.get('/api/slips')[0]['returns'], [])

    def test_supplier_registration_edit_and_legacy_import(self):
        self.create()
        with self.app.db() as c:
            c.execute('DELETE FROM suppliers')
        imported = self.get('/api/suppliers')
        self.assertEqual(imported[0]['name'], '공급사 A')
        self.post('/api/suppliers', dict(imported[0], name='변경된 공급사', phone='010-1111-2222'))
        self.assertEqual([s['name'] for s in self.get('/api/suppliers')], ['변경된 공급사'])
        self.assertEqual(self.get('/api/slips')[0]['supplier'], '공급사 A')
        self.assert_error(400, '/api/suppliers', {'name': '변경된 공급사'})

    def test_settings_and_excel_with_item_history(self):
        self.assertIn('AS'.encode(), self.req('/'))
        self.post('/api/settings', {'company': '테스트 업체'})
        self.assertEqual(self.get('/api/settings')['company'], '테스트 업체')
        s = self.create()
        s = self.ret(s, items=[{'index': 0, 'qty': 1}], result='교환')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            for name in z.namelist():
                ET.fromstring(z.read(name))
            sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = [[c.find('s:is/s:t', ns).text for c in row] for row in sheet.findall('s:sheetData/s:row', ns)]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[1][9], '001234')
            self.assertEqual(rows[1][15:17], ['1', '1'])
            self.assertIn('2026-10-20 1개 교환', rows[1][17])
            self.assertIsNone(rows[2][17])

    def test_products_registration_and_rename_preserves_old_slip(self):
        s = self.create()
        product = next(p for p in self.get('/api/products') if p['name'] == '메모리 DDR5')
        self.post('/api/products', dict(product, name='삼성 DDR5 16GB'))
        names = [p['name'] for p in self.get('/api/products')]
        self.assertIn('삼성 DDR5 16GB', names)
        self.assertNotIn('메모리 DDR5', names)
        self.assertEqual(self.get('/api/slips')[0]['items'][0]['name'], '메모리 DDR5')
        s['note'] = '기존 품명 유지'
        self.assertEqual(self.post('/api/slips', s)['items'][0]['name'], '메모리 DDR5')
        self.assert_error(400, '/api/products', {'name': '삼성 DDR5 16GB'})
        self.assert_error(400, '/api/products', {'name': ' '})
        self.assert_error(400, '/api/products', {'name': 'A' * 121})

    def test_legacy_model_is_combined_once_and_catalogue_is_seeded(self):
        s = self.ret(self.create(), items=[{'index': 0, 'qty': 1}])
        s['items'][0].update(name='메모리', model='DDR5')
        with self.app.db() as c:
            c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(s), s['id']))
            c.execute('DELETE FROM products')
        migrated = self.get('/api/slips')[0]
        self.assertEqual(migrated['items'][0]['name'], '메모리 DDR5')
        self.assertNotIn('model', migrated['items'][0])
        self.assertEqual(migrated['items'][0]['returnedQty'], 1)
        self.assertIn('메모리 DDR5', [p['name'] for p in self.get('/api/products')])
        saved = self.post('/api/slips', migrated)
        self.assertEqual(saved['items'][0]['name'], '메모리 DDR5')
        self.assertEqual(saved['returns'], s['returns'])
        self.assertEqual(self.get('/api/slips')[0]['items'][0]['name'], '메모리 DDR5')

    def test_shipping_metadata_and_excel_preserve_tracking_zeros(self):
        s = self.create()
        s.update(shippedDate='2026-01-31', trackingNumber='000012345678', shippedMonth='틀린 월')
        saved = self.post('/api/slips', s)
        self.assertEqual(saved['shippedMonth'], '1월')
        self.assertEqual(saved['trackingNumber'], '000012345678')
        self.assertEqual(self.get('/api/slips')[0]['shippedDate'], '2026-01-31')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = [[c.find('s:is/s:t', ns).text for c in row] for row in root.findall('s:sheetData/s:row', ns)]
            exported = dict(zip(rows[0], rows[1]))
            self.assertEqual(exported['출고일'], '2026-01-31')
            self.assertEqual(exported['출고 월'], '1월')
            self.assertEqual(exported['출고 송장번호'], '000012345678')
            self.assertEqual(exported['품명'], '메모리 DDR5')
            self.assertNotIn('모델명', exported)
            self.assertIn('받는이 전화번호', exported)
        saved.update(shippedDate='', trackingNumber='')
        cleared = self.post('/api/slips', saved)
        self.assertEqual(cleared['shippedMonth'], '')
        cleared['shippedDate'] = '2026-02-30'
        self.assert_error(400, '/api/slips', cleared)

    def test_removed_input_fields_are_not_accepted(self):
        self.post('/api/settings', {'company': '새 업체', 'companyPhone': '010-0000-0000',
                                    'address': '주소', 'business': '사업자번호'})
        settings = self.get('/api/settings')
        self.assertEqual(settings['companyPhone'], '010-0000-0000')
        self.assertNotIn('address', settings)
        self.assertNotIn('business', settings)
        self.assertNotIn('address', self.get('/api/suppliers')[0])
        s = self.create()
        s.update(address='새 주소', business='새 번호', supplierAddress='받는이 주소')
        saved = self.post('/api/slips', s)
        for key in ('address', 'business', 'supplierAddress'):
            self.assertNotIn(key, saved)

    def test_manual_product_name_survives_edit_return_and_excel(self):
        self.get('/api/products')
        s = self.create()
        manual = '수기 제품 <GPU> "미등록"'
        s['items'][1]['name'] = manual
        s = self.post('/api/slips', s)
        self.assertEqual(self.get('/api/slips')[0]['items'][1]['name'], manual)
        self.assertNotIn(manual, [p['name'] for p in self.get('/api/products')])
        s = self.ret(s, items=[{'index': 1, 'qty': 1}])
        self.assertEqual(s['items'][1]['name'], manual)
        self.assertEqual(s['items'][1]['returnedQty'], 1)
        s['note'] = '수기 품명 반환 이력 보존'
        self.assertEqual(self.post('/api/slips', s)['items'][1]['name'], manual)
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            self.assertIn(manual, [t.text for t in root.iter()])

    def test_manual_product_validation_still_rejects_empty_and_long_names(self):
        for name in ('', '   ', 'A' * 121):
            s = self.create()
            s['items'][0]['name'] = name
            self.assert_error(400, '/api/slips', s)

    def test_unknown_path_and_invalid_record(self):
        self.assert_error(400, '/api/slips', {'supplier': '', 'date': '2026-10-19'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.req('/../app.py')
        self.assertEqual(err.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
