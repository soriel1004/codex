from datetime import date
from unittest.mock import patch
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
            for table in ('slips', 'settings', 'suppliers', 'products', 'receipt_options', 'app_meta'):
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

    def create(self, date='2026-10-19'):
        return self.post('/api/slips', {
            'date': date, 'supplier': '공급사 A', 'company': '테스트 업체', 'kind': 'AS 발송', 'status': '발송',
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

    def test_edit_changes_product_fields_and_preserves_receipt_links(self):
        s = self.ret(self.create(), items=[{'index':1,'qty':1}])
        stale = copy.deepcopy(s)
        s['items'][1].update(name='수정된 메인보드',serial='NEW-OUT',qty=2,productionMonth='2025-11',fault='수정 증상')
        changed = self.post('/api/slips',s)
        self.assertEqual(changed['items'][1]['name'],'수정된 메인보드')
        self.assertEqual(changed['items'][1]['serial'],'NEW-OUT')
        self.assertEqual(changed['items'][1]['returnedQty'],1)
        self.assert_error(409,'/api/slips',stale)
        changed['items'].reverse()
        reordered = self.post('/api/slips',changed)
        self.assertEqual(reordered['items'][1]['name'],'수정된 메인보드')
        self.assertEqual(reordered['returns'][0]['items'][0]['index'],1)
        reordered['returns']=[]
        reordered['note']='메모만 변경해도 기존 입고내역 유지'
        saved=self.post('/api/slips',reordered)
        self.assertEqual(saved['items'][1]['returnedQty'],1)
        self.assertEqual(saved['status'],'부분반환')

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

    def test_settings_and_excel_with_item_receipt_fields(self):
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
            first, second = (dict(zip(rows[0], row)) for row in rows[1:3])
            self.assertEqual(first['출고 시리얼번호'], '001234')
            self.assertIsNone(first['입고 시리얼번호'])
            self.assertEqual(first['반환수량'], '1')
            self.assertEqual(first['미반환수량'], '1')
            self.assertEqual(first['입고처리'], '교환')
            self.assertEqual(first['입고일'], '2026-10-20')
            self.assertIsNone(second['입고처리'])
            self.assertIsNone(second['입고일'])
            self.assertNotIn('품명별 반환이력', rows[0])

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
        self.assertEqual(saved['shippedMonth'], '10월')
        self.assertEqual(saved['trackingNumber'], '000012345678')
        self.assertEqual(self.get('/api/slips')[0]['shippedDate'], '2026-01-31')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = [[c.find('s:is/s:t', ns).text for c in row] for row in root.findall('s:sheetData/s:row', ns)]
            exported = dict(zip(rows[0], rows[1]))
            self.assertEqual(exported['접수일 (출고일)'], '2026-10-19')
            self.assertEqual(exported['출고월'], '10월')
            self.assertNotIn('출고일',exported)
            self.assertEqual(exported['출고 송장번호'], '000012345678')
            self.assertEqual(exported['품명'], '메모리 DDR5')
            self.assertNotIn('모델명', exported)
            self.assertNotIn('받는이 전화번호', exported)
            self.assertNotIn('보낸이 전화번호', exported)
            self.assertEqual(rows[0][1:5], ['접수일 (출고일)','출고월','택배사','출고 송장번호'])
        saved.update(shippedDate='', trackingNumber='')
        cleared = self.post('/api/slips', saved)
        self.assertEqual(cleared['shippedMonth'], '10월')
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

    def test_courier_requesters_production_month_and_excel(self):
        s = self.create()
        self.assertEqual(s['requesters'], [])
        self.assertEqual(s['courier'], '')
        self.assertEqual(s['items'][0]['productionMonth'], '')
        s.update(courier='CJ대한통운', requesters=['대일', 'GN'])
        s['items'][0]['productionMonth'] = '2025-09'
        s = self.post('/api/slips', s)
        self.assertEqual(s['requesters'], ['GN', '대일'])
        self.assertEqual(s['items'][0]['productionMonth'], '2025-09')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = [[c.find('s:is/s:t', ns).text for c in row] for row in root.findall('s:sheetData/s:row', ns)]
            record = dict(zip(rows[0], rows[1]))
            self.assertEqual(record['택배사'], 'CJ대한통운')
            self.assertEqual(record['의뢰업체'], 'GN · 대일')
            self.assertEqual(record['생산 년월'], '2025-09')
        s['requesters'] = []
        self.assertEqual(self.post('/api/slips', s)['requesters'], [])

    def test_invalid_requesters_and_production_month(self):
        s = self.create()
        for selection in (['기타'], ['GN', 'GN'], 'GN'):
            bad = dict(s, requesters=selection)
            self.assert_error(400, '/api/slips', bad)
        for month in ('2025-13', '2025-01-01', '2025-1'):
            bad = copy.deepcopy(s)
            bad['items'][0]['productionMonth'] = month
            self.assert_error(400, '/api/slips', bad)

    def test_replacement_serials_preserve_outbound_and_partial_history(self):
        s = self.create()
        s = self.ret(s, items=[{'index': 0, 'qty': 1, 'receivedSerial': '000NEW-A'}])
        self.assertEqual(s['items'][0]['serial'], '001234')
        self.assertEqual(s['items'][0]['receivedSerial'], '000NEW-A')
        s = self.ret(s, items=[{'index': 0, 'qty': 1, 'receivedSerial': '000NEW-B'}])
        self.assertEqual(s['items'][0]['receivedSerial'], '000NEW-A\n000NEW-B')
        self.assertEqual(s['returns'][0]['items'][0]['receivedSerial'], '000NEW-A')
        s = self.ret(s, all=True)
        self.assertEqual(s['status'], '반환완료')
        s = self.post('/api/return-serials', {'id': s['id'], 'version': s['version'], 'eventIndex': 0,
                      'items': [{'index': 0, 'receivedSerial': '000CORRECTED'}]})
        self.assertEqual(s['items'][0]['receivedSerial'], '000CORRECTED\n000NEW-B')
        self.assertEqual(s['items'][0]['serial'], '001234')
        self.assertEqual(s['status'], '반환완료')
        self.assertEqual(s['returns'][0]['items'][0]['qty'], 1)
        self.assertEqual(s['returns'][0]['date'], '2026-10-20')
        self.assertEqual(self.get('/api/slips')[0]['items'][0]['receivedSerial'], '000CORRECTED\n000NEW-B')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            xml = z.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('000CORRECTED', xml)
            self.assertIn('000NEW-B', xml)
            self.assertIn('001234', xml)

    def test_serial_corrections_validate_version_and_event_membership(self):
        s = self.ret(self.create(), items=[{'index': 1, 'qty': 1}])
        base = {'id': s['id'], 'version': s['version'], 'eventIndex': 0}
        for items in ([], [{'index': 0, 'receivedSerial': 'WRONG'}],
                      [{'index': 1, 'receivedSerial': 'A'}, {'index': 1, 'receivedSerial': 'B'}],
                      [{'index': 1, 'receivedSerial': 'A' * 401}]):
            self.assert_error(400, '/api/return-serials', dict(base, items=items))
        self.post('/api/return-serials', dict(base, items=[{'index': 1, 'receivedSerial': '000REPLACEMENT'}]))
        self.assert_error(409, '/api/return-serials', dict(base, items=[{'index': 1, 'receivedSerial': 'STALE'}]))
        self.assertEqual(self.get('/api/slips')[0]['items'][1]['receivedSerial'], '000REPLACEMENT')

    def test_daily_slip_numbers_and_issued_number_preservation(self):
        first, second = self.create(), self.create()
        self.assertEqual(first['number'], '261019-01')
        self.assertEqual(second['number'], '261019-02')
        self.assertEqual(self.create('2026-10-20')['number'], '261020-01')
        first['date'] = '2026-10-21'
        self.assertEqual(self.post('/api/slips', first)['number'], '261019-01')
        self.assertEqual(self.create()['number'], '261019-03')

    def test_concurrent_creations_have_distinct_daily_numbers(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            created = list(pool.map(lambda _: self.create(), range(4)))
        self.assertEqual(sorted(s['number'] for s in created), ['261019-01','261019-02','261019-03','261019-04'])

    def test_legacy_issued_number_is_kept_and_counted(self):
        s = self.create()
        s['number'] = 'AS-20261019-00001'
        with self.app.db() as c:
            c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(s), s['id']))
        self.assertEqual(self.get('/api/slips')[0]['number'], 'AS-20261019-00001')
        self.assertEqual(self.create()['number'], '261019-02')

    def test_excel_order_and_dropdown_with_partial_item_receipt_dates(self):
        s = self.create()
        s = self.ret(s, items=[{'index': 0, 'qty': 1, 'receivedSerial': '000NEW'}], result='교환')
        s = self.ret(s, date='2026-10-21', items=[{'index': 1, 'qty': 1}], result='수리')
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = [[c.find('s:is/s:t', ns).text for c in row] for row in root.findall('s:sheetData/s:row', ns)]
            headers = rows[0]
            self.assertEqual(headers[:5], ['전표번호','접수일 (출고일)','출고월','택배사','출고 송장번호'])
            out_index = headers.index('출고 시리얼번호')
            self.assertEqual(headers[out_index + 1], '입고 시리얼번호')
            self.assertEqual(headers[-2:], ['입고처리','입고일'])
            for forbidden in ('받는이 전화번호','보낸이 전화번호','품명별 반환이력','처리 결과','최근 반환일'):
                self.assertNotIn(forbidden, headers)
            first, second, pending = [dict(zip(headers, row)) for row in rows[1:]]
            self.assertEqual(first['입고 시리얼번호'], '000NEW')
            self.assertEqual(first['입고처리'], '교환')
            self.assertEqual(first['입고일'], '2026-10-20')
            self.assertIsNone(second['입고 시리얼번호'])
            self.assertEqual(second['입고처리'], '수리')
            self.assertEqual(second['입고일'], '2026-10-21')
            self.assertIsNone(pending['입고일'])
            validation = root.find('s:dataValidations/s:dataValidation', ns)
            self.assertEqual(validation.attrib['sqref'], 'U2:U4')
            self.assertEqual(validation.attrib['type'], 'list')
            self.assertEqual(validation.find('s:formula1', ns).text, 'ReceiptOptions')
            choices_sheet = ET.fromstring(z.read('xl/worksheets/sheet2.xml'))
            options = [t.text for t in choices_sheet.findall('.//s:t', ns)]
            self.assertEqual(options, self.get('/api/receipt-options'))
            self.assertIn('교환', options)

    def test_receipt_treatment_selection_and_history_correction(self):
        s = self.create()
        self.assert_error(400, '/api/returns', {'id':s['id'], 'version':s['version'], 'date':'2026-10-20', 'all':True, 'result':'임의 값'})
        s = self.ret(s, all=True, result='교환')
        s = self.post('/api/return-serials', {'id':s['id'], 'version':s['version'], 'eventIndex':0,
                      'result':'수리', 'items':[{'index':i, 'receivedSerial':''} for i in range(3)]})
        self.assertEqual(s['result'], '수리')
        self.assertEqual(s['items'][0]['receiptTreatment'], '수리')
        self.assertEqual(s['items'][0]['receivedDate'], '2026-10-20')
        self.assertEqual(s['status'], '반환완료')

    def test_custom_receipt_options_crud_preserves_history_and_empty_list(self):
        name = '유상 교환 완료'
        self.post('/api/receipt-options', {'name': name})
        row = next(x for x in self.get('/api/receipt-options/manage') if x['name'] == name)
        s = self.ret(self.create(), all=True, result=name)
        self.post('/api/receipt-options', dict(row, name='수리 후 출고'))
        choices = self.get('/api/receipt-options')
        self.assertIn('수리 후 출고', choices)
        self.assertNotIn(name, choices)
        self.assertEqual(self.get('/api/slips')[0]['result'], name)
        # Old values stay valid when saving the same historic record after renaming.
        s = self.post('/api/return-serials', {'id':s['id'], 'version':s['version'], 'eventIndex':0,
                      'items':[{'index':i,'receivedSerial':''} for i in range(3)]})
        self.assertEqual(s['result'], name)
        for option in self.get('/api/receipt-options/manage'):
            self.post('/api/receipt-options/delete', {'id':option['id']})
        self.assertEqual(self.get('/api/receipt-options'), [])
        self.assertEqual(self.get('/api/receipt-options/manage'), [])
        self.assertEqual(self.get('/api/slips')[0]['result'], name)
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            workbook = ET.fromstring(z.read('xl/workbook.xml'))
            self.assertIn('$A$1:$A$1', workbook.find('s:definedNames/s:definedName',ns).text)

    def test_receipt_options_validate_names_and_deleted_choices(self):
        for name in (' ', 'A'*61, '줄바꿈\n항목'):
            self.assert_error(400, '/api/receipt-options', {'name':name})
        self.assert_error(400, '/api/receipt-options', {'name':'수리'})
        self.assert_error(400, '/api/receipt-options/delete', {'id':999999})
        custom = '사용자 정의 항목'
        self.post('/api/receipt-options', {'name':custom})
        row = next(x for x in self.get('/api/receipt-options/manage') if x['name']==custom)
        self.post('/api/receipt-options/delete', {'id':row['id']})
        s = self.create()
        self.assert_error(400, '/api/returns', {'id':s['id'],'version':s['version'],'date':'2026-10-20','all':True,'result':custom})

    def test_excel_custom_option_list_supports_commas_and_over_255_characters(self):
        names = ['유상, 교환 "완료" & 확인'] + [f'사용자 정의 입고 항목 {i:02d}' for i in range(25)]
        for name in names:
            self.post('/api/receipt-options', {'name':name})
        self.create()
        with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
            ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            for filename in z.namelist():
                ET.fromstring(z.read(filename))
            sheet = ET.fromstring(z.read('xl/worksheets/sheet2.xml'))
            choices = [cell.text for cell in sheet.findall('.//s:t',ns)]
            self.assertEqual(choices, self.get('/api/receipt-options'))
            self.assertGreater(len(','.join(choices)),255)
            for name in names:
                self.assertIn(name, choices)
            workbook = ET.fromstring(z.read('xl/workbook.xml'))
            self.assertEqual(workbook.find('s:sheets',ns)[1].attrib['state'],'hidden')
            self.assertIn('$A$'+str(len(choices)), workbook.find('s:definedNames/s:definedName',ns).text)
            main = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(main.find('s:dataValidations/s:dataValidation/s:formula1',ns).text,'ReceiptOptions')

    def test_editor_can_change_completed_slip_and_receipt_fields(self):
        s=self.ret(self.create(),all=True,result='교환')
        s.update(number='CUSTOM-001',date='2026-10-18',company='수정 보낸이',companyPhone='010-9999-9999',manager='새 담당자',courier='로젠',trackingNumber='000UPDATED',requesters=['GN'])
        s['items'][0].update(name='수정 품명',serial='000CHANGED-OUT',productionMonth='2025-12',fault='수정 증상')
        s['receiptEdits']=[{'index':i,'eventIndex':0,'date':'2026-10-22','qty':int(item['qty']),'result':'수리','receivedSerial':f'000NEW-{i}'} for i,item in enumerate(s['items'])]
        saved=self.post('/api/slips',s)
        self.assertEqual(saved['number'],'CUSTOM-001')
        self.assertEqual(saved['date'],'2026-10-18')
        self.assertEqual(saved['shippedMonth'],'10월')
        self.assertEqual(saved['company'],'수정 보낸이')
        self.assertEqual(saved['items'][0]['serial'],'000CHANGED-OUT')
        self.assertEqual(saved['items'][0]['receivedSerial'],'000NEW-0')
        self.assertEqual(saved['items'][0]['receivedDate'],'2026-10-22')
        self.assertEqual(saved['result'],'수리')
        self.assertEqual(saved['status'],'반환완료')
        self.assertEqual(saved['elapsedDays'],4)

    def test_editor_can_reopen_add_delete_items_and_receipts(self):
        s=self.ret(self.create(),all=True)
        s['items'][0]['qty']=3
        s['status']='부분반환'
        s=self.post('/api/slips',s)
        self.assertEqual(s['items'][0]['remainingQty'],1)
        # Explicit receipt editing permits removing a product and its receipt rows.
        s['items']=s['items'][1:]
        s['receiptEdits']=[]
        s['status']='발송'
        s=self.post('/api/slips',s)
        self.assertEqual(len(s['items']),2)
        self.assertEqual(s['returns'],[])
        self.assertEqual(s['status'],'발송')
        s['items'].append({'name':'추가 제품','qty':1,'sourceIndex':None})
        s=self.post('/api/slips',s)
        self.assertEqual(len(s['items']),3)

    def test_editor_rejects_conflicting_receipt_quantities_dates_and_numbers(self):
        s=self.ret(self.create(),all=True)
        invalid=copy.deepcopy(s)
        invalid['items'][0]['qty']=1
        self.assert_error(400,'/api/slips',invalid)
        invalid=copy.deepcopy(s)
        invalid['date']='2026-10-21'
        self.assert_error(400,'/api/slips',invalid)
        other=self.create()
        s['number']=other['number']
        self.assert_error(400,'/api/slips',s)

    def test_elapsed_days_freezes_on_final_return_and_is_exported(self):
        s=self.create()
        with patch.object(self.app,'korea_today',return_value=date(2026,10,25)):
            self.assertEqual(self.get('/api/slips')[0]['elapsedDays'],6)
            s=self.ret(s,items=[{'index':0,'qty':1}],date='2026-10-20')
            self.assertEqual(s['elapsedDays'],6)
            s=self.ret(s,all=True,date='2026-10-23')
            self.assertEqual(s['elapsedDays'],4)
            self.assertEqual(self.get('/api/slips')[0]['elapsedDays'],4)
            with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
                root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                rows=[[cell.find('s:is/s:t',ns).text for cell in row] for row in root.findall('s:sheetData/s:row',ns)]
                record=dict(zip(rows[0],rows[1]))
                self.assertEqual(record['경과일수'],'4')
                self.assertEqual(record['접수일 (출고일)'],'2026-10-19')
                self.assertEqual(rows[0][2],'출고월')

    def test_excel_elapsed_days_stop_for_fully_received_product(self):
        s=self.create()
        with patch.object(self.app,'korea_today',return_value=date(2026,10,25)):
            s=self.ret(s,items=[{'index':0,'qty':2}],date='2026-10-20')
            self.assertEqual(s['elapsedDays'],6)
            self.assertEqual(s['items'][0]['elapsedDays'],1)
            self.assertEqual(s['items'][1]['elapsedDays'],6)
            with zipfile.ZipFile(io.BytesIO(self.req('/api/export'))) as z:
                root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                rows=[[cell.find('s:is/s:t',ns).text for cell in row] for row in root.findall('s:sheetData/s:row',ns)]
                self.assertEqual(dict(zip(rows[0],rows[1]))['경과일수'],'1')
                self.assertEqual(dict(zip(rows[0],rows[2]))['경과일수'],'6')

    def test_unknown_path_and_invalid_record(self):
        self.assert_error(400, '/api/slips', {'supplier': '', 'date': '2026-10-19'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.req('/../app.py')
        self.assertEqual(err.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
