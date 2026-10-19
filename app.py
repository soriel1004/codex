"""AS 전표 관리 — Python 표준 라이브러리만 사용합니다."""
import io, json, os, re, sqlite3, zipfile
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('AS_DATA_DIR', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
RECEIPT_OPTIONS = ('수리', '교환', '정상 판정', '수리 불가', '반송', '기타')

def db():
    c = sqlite3.connect(DATA / 'as.db')
    c.execute('CREATE TABLE IF NOT EXISTS slips (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, phone TEXT NOT NULL, address TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)')
    c.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)')
    return c

def next_slip_number(c, date):
    # The caller holds a write transaction. Preserve issued numbers and reserve
    # suffixes even when an existing slip's receipt date has later been edited.
    prefix = datetime.strptime(date, '%Y-%m-%d').strftime('%y%m%d')
    count, highest = 0, 0
    for (body,) in c.execute('SELECT body FROM slips'):
        r = json.loads(body)
        count += r.get('date') == date
        match = re.fullmatch(re.escape(prefix) + r'-(\d+)', r.get('number', ''))
        if match:
            highest = max(highest, int(match[1]))
    return f'{prefix}-{max(count, highest) + 1:02d}'


def normalize(r):
    # Upgrade old completed slips in memory without losing their original return date.
    if 'returns' not in r:
        r['returns'] = ([{'date': r.get('returned', ''), 'result': r.get('result', ''),
                          'items': [{'index': i, 'qty': int(item['qty'])} for i, item in enumerate(r['items'])]}]
                        if r.get('status') == '반환완료' else [])
    r.setdefault('version', 0)
    r.setdefault('shippedDate', '')
    r.setdefault('trackingNumber', '')
    r.setdefault('courier', '')
    r.setdefault('requesters', [])
    r['shippedMonth'] = str(int(r['shippedDate'][5:7])) + '월' if r['shippedDate'] else ''
    for i, item in enumerate(r['items']):
        # Preserve old model information as part of the unified product name.
        if item.get('model'):
            item['name'] = ' '.join(filter(None, (item.get('name', ''), item['model'])))
        item.pop('model', None)
        item.setdefault('productionMonth', '')
        item['receivedSerial'] = '\n'.join(x.get('receivedSerial', '') for event in r['returns'] for x in event['items'] if x['index'] == i and x.get('receivedSerial'))
        item['returnedQty'] = sum(int(x['qty']) for event in r['returns'] for x in event['items'] if x['index'] == i)
        item['remainingQty'] = int(item['qty']) - item['returnedQty']
        received = [(index, event) for index, event in enumerate(r['returns']) if any(x['index'] == i for x in event['items'])]
        latest = max(received, key=lambda pair: (pair[1]['date'], pair[0]))[1] if received else {}
        item['receiptTreatment'] = latest.get('result', '')
        item['receivedDate'] = latest.get('date', '')
    if all(i['remainingQty'] == 0 for i in r['items']):
        r['status'] = '반환완료'
    elif r['returns']:
        r['status'] = '부분반환'
    r['returned'] = max((e['date'] for e in r['returns']), default='')
    if r['returns']:
        r['result'] = max(enumerate(r['returns']), key=lambda pair: (pair[1]['date'], pair[0]))[1].get('result', '')
    return r


def records():
    with db() as c:
        return [normalize(dict(json.loads(body), id=id)) for id, body in c.execute('SELECT id, body FROM slips ORDER BY id DESC')]


def suppliers():
    # Seed once: renaming a supplier must not recreate its old name from historical slips.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("SELECT 1 FROM app_meta WHERE key='suppliers_seeded'").fetchone():
            for (body,) in c.execute('SELECT body FROM slips ORDER BY id DESC').fetchall():
                r = json.loads(body)
                c.execute('INSERT OR IGNORE INTO suppliers(name,phone,address) VALUES(?,?,?)',
                          (r['supplier'], r.get('phone', ''), r.get('supplierAddress', '')))
            c.execute("INSERT INTO app_meta VALUES('suppliers_seeded','1')")
        return [dict(id=i, name=n, phone=p) for i,n,p in c.execute('SELECT id,name,phone FROM suppliers ORDER BY name')]


def products():
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("SELECT 1 FROM app_meta WHERE key='products_seeded'").fetchone():
            for (body,) in c.execute('SELECT body FROM slips ORDER BY id DESC').fetchall():
                for item in normalize(json.loads(body))['items']:
                    c.execute('INSERT OR IGNORE INTO products(name) VALUES(?)', (item['name'],))
            c.execute("INSERT INTO app_meta VALUES('products_seeded','1')")
        return [dict(id=i, name=n) for i,n in c.execute('SELECT id,name FROM products ORDER BY name')]


def valid_date(value):
    try:
        if datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d') != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('올바른 날짜를 입력하세요. (YYYY-MM-DD)')
    return value


def receipt_treatment(value, previous=''):
    if not isinstance(value, str) or (value not in ('', *RECEIPT_OPTIONS) and value != previous):
        raise ValueError('입고처리는 목록에서 선택하세요.')
    return value


def clean_received_serial(value):
    if not isinstance(value, str) or len(value) > 400:
        raise ValueError('입고 시리얼은 400자 이내로 입력하세요. 여러 개는 쉼표나 줄바꿈으로 구분하세요.')
    return value.strip()


def update_return_serials(r, data):
    event_index = data.get('eventIndex')
    if type(event_index) is not int or not 0 <= event_index < len(r['returns']):
        raise ValueError('반환 이력을 선택하세요.')
    event = r['returns'][event_index]
    treatment = receipt_treatment(data.get('result', event.get('result', '')), event.get('result', ''))
    updates = data.get('items', [])
    indexes = [x.get('index') for x in updates]
    if any(type(i) is not int for i in indexes) or len(set(indexes)) != len(indexes) or set(indexes) != {x['index'] for x in event['items']}:
        raise ValueError('반환 이력의 품목이 일치하지 않습니다.')
    serials = {x['index']: clean_received_serial(x.get('receivedSerial', '')) for x in updates}
    event['result'] = treatment
    for item in event['items']:
        item['receivedSerial'] = serials[item['index']]
    r['version'] += 1
    return normalize(r)


def apply_return(r, data):
    date = valid_date(data.get('date', ''))
    treatment = receipt_treatment(data.get('result', ''))
    if date < r['date']:
        raise ValueError('반환일은 접수일 이후여야 합니다.')
    selected = ([{'index': i, 'qty': item['remainingQty']} for i,item in enumerate(r['items']) if item['remainingQty']]
                if data.get('all') else data.get('items', []))
    if not selected:
        raise ValueError('반환할 품목을 선택하세요.')
    seen = set()
    clean = []
    for x in selected:
        i, qty = x['index'], x['qty']
        if type(i) is not int or i < 0 or i >= len(r['items']) or i in seen:
            raise ValueError('반환 품목이 올바르지 않습니다.')
        if type(qty) is not int or qty < 1 or qty > r['items'][i]['remainingQty']:
            raise ValueError('반환 수량은 미반환 수량 이내의 정수여야 합니다.')
        serial = clean_received_serial(x.get('receivedSerial', ''))
        seen.add(i); clean.append({'index': i, 'qty': qty, **({'receivedSerial': serial} if serial else {})})
    r['returns'].append({'date': date, 'result': treatment, 'items': clean})
    r['result'] = treatment
    r['version'] += 1
    return normalize(r)


def xlsx(rows):
    headers = ['전표번호', '접수일', '출고일', '출고 월', '택배사', '출고 송장번호',
               '의뢰업체', '받는이', '보낸이', '품명', '생산 년월', '출고 시리얼번호',
               '입고 시리얼번호', '수량', '불량 증상', '구분', '진행상태', '비고',
               '반환수량', '미반환수량', '입고처리', '입고일']
    table = [headers]
    for r in rows:
        for item in r['items']:
            table.append([
                r['number'], r['date'], r.get('shippedDate', ''), r.get('shippedMonth', ''),
                r.get('courier', ''), r.get('trackingNumber', ''), ' · '.join(r.get('requesters', [])),
                r['supplier'], r.get('company', ''), item.get('name', ''), item.get('productionMonth', ''),
                item.get('serial', ''), item.get('receivedSerial', ''), item.get('qty', 1),
                item.get('fault', ''), r['kind'], r['status'], r.get('note', ''), item['returnedQty'],
                item['remainingQty'], item.get('receiptTreatment', ''), item.get('receivedDate', '')])
    def col(i):
        s = ''
        while i:
            i, n = divmod(i-1,26); s = chr(65+n)+s
        return s
    xmlrows = []
    for n, row in enumerate(table,1):
        cells = []
        for i,v in enumerate(row,1):
            ref = f'{col(i)}{n}'
            # Inline strings keep serial numbers, leading zeros and formula-like input literal.
            value = ''.join(ch for ch in str(v) if ch in '\t\n\r' or ord(ch)>=32)
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>')
        xmlrows.append(f'<row r="{n}">{"".join(cells)}</row>')
    treatment_column = col(len(headers) - 1)
    validation = ''
    if len(table) > 1:
        choices = escape('"' + ','.join(RECEIPT_OPTIONS) + '"')
        validation = (f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="1" '
                      f'errorStyle="stop" sqref="{treatment_column}2:{treatment_column}{len(table)}">'
                      f'<formula1>{choices}</formula1></dataValidation></dataValidations>')
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="AS 누적내역" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="'+str(len(headers))+'" width="22" customWidth="1"/></cols><sheetData>'+''.join(xmlrows)+'</sheetData><autoFilter ref="A1:'+col(len(headers))+str(len(table))+'"/>'+validation+'</worksheet>')
    return out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def send(self, body, content='application/json; charset=utf-8', status=200):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type',content)
        self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        if content.startswith('application/vnd'): self.send_header('Content-Disposition','attachment; filename="AS-history.xlsx"')
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/slips': return self.send(records())
        if path=='/api/receipt-options': return self.send(list(RECEIPT_OPTIONS))
        if path=='/api/suppliers': return self.send(suppliers())
        if path=='/api/products': return self.send(products())
        if path=='/api/settings':
            with db() as c: row=c.execute('SELECT body FROM settings WHERE id=1').fetchone()
            return self.send({k:v for k,v in json.loads(row[0]).items() if k in ('company', 'companyPhone', 'manager')} if row else {})
        if path=='/api/export': return self.send(xlsx(records()),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path not in files: return self.send({'error':'찾을 수 없습니다.'},status=404)
        file=ROOT/'static'/files[path]
        return self.send(file.read_bytes(), {'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[file.suffix[1:]])
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length',0))
            if length>200000: raise ValueError('입력 내용이 너무 큽니다.')
            data=json.loads(self.rfile.read(length))
            path=urlparse(self.path).path
            if path=='/api/settings':
                data = {k: str(v)[:60] for k,v in data.items() if k in ('company', 'companyPhone', 'manager')}
                with db() as c: c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(data,ensure_ascii=False),))
                return self.send({'ok':True})
            if path == '/api/suppliers':
                name = str(data.get('name', '')).strip()
                if not name or len(name) > 60:
                    raise ValueError('받는이 업체명을 1~60자로 입력하세요.')
                with db() as c:
                    try:
                        if data.get('id'):
                            if not c.execute('SELECT 1 FROM suppliers WHERE id=?', (data['id'],)).fetchone():
                                raise ValueError('받는이가 없습니다.')
                            c.execute('UPDATE suppliers SET name=?,phone=? WHERE id=?',
                                      (name, str(data.get('phone',''))[:40], data['id']))
                        else:
                            c.execute('INSERT INTO suppliers(name,phone,address) VALUES(?,?,?)',
                                      (name, str(data.get('phone',''))[:40], ''))
                    except sqlite3.IntegrityError:
                        raise ValueError('이미 등록된 받는이 업체명입니다.')
                return self.send({'ok': True})
            if path == '/api/products':
                name = str(data.get('name', '')).strip()
                if not name or len(name) > 120:
                    raise ValueError('품명을 1~120자로 입력하세요.')
                with db() as c:
                    try:
                        if data.get('id'):
                            if not c.execute('SELECT 1 FROM products WHERE id=?', (data['id'],)).fetchone():
                                raise ValueError('등록된 품명이 없습니다.')
                            c.execute('UPDATE products SET name=? WHERE id=?', (name, data['id']))
                        else:
                            c.execute('INSERT INTO products(name) VALUES(?)', (name,))
                    except sqlite3.IntegrityError:
                        raise ValueError('이미 등록된 품명입니다.')
                return self.send({'ok': True})
            if path not in ('/api/slips', '/api/returns', '/api/return-serials'):
                return self.send({'error':'찾을 수 없습니다.'},status=404)
            with db() as c:
                # Serialize read/validate/write so simultaneous returns cannot exceed sent quantities.
                c.execute('BEGIN IMMEDIATE')
                id = data.get('id')
                old = None
                if id:
                    row = c.execute('SELECT body FROM slips WHERE id=?', (id,)).fetchone()
                    if not row:
                        return self.send({'error':'전표가 없습니다.'},status=404)
                    old = normalize(json.loads(row[0]))
                    if data.get('version', 0) != old['version']:
                        return self.send({'error':'다른 작업에서 전표가 변경되었습니다. 목록을 새로고침 후 다시 처리하세요.'},status=409)
                if path == '/api/return-serials':
                    if not old:
                        raise ValueError('반환 이력이 있는 전표를 선택하세요.')
                    result = update_return_serials(old, data)
                elif path == '/api/returns':
                    if not old:
                        raise ValueError('반환할 전표를 선택하세요.')
                    result = apply_return(old, data)
                else:
                    if not data.get('supplier','').strip() or not data.get('date'):
                        raise ValueError('받는이와 접수일을 입력하세요.')
                    valid_date(data['date'])
                    if data.get('shippedDate'):
                        valid_date(data['shippedDate'])
                    requesters = data.get('requesters', old.get('requesters', []) if old else [])
                    if not isinstance(requesters, list) or any(x not in ('GN', '대일') for x in requesters) or len(set(requesters)) != len(requesters):
                        raise ValueError('의뢰업체는 GN, 대일 중 선택하거나 선택하지 않을 수 있습니다.')
                    if not isinstance(data.get('courier', ''), str) or len(data.get('courier', '')) > 40:
                        raise ValueError('택배사는 40자 이내로 입력하세요.')
                    if len(str(data.get('trackingNumber', ''))) > 60:
                        raise ValueError('출고 송장번호는 60자 이내로 입력하세요.')
                    if not (old and old['supplier'] == data['supplier']) and not c.execute('SELECT 1 FROM suppliers WHERE name=?', (data['supplier'],)).fetchone():
                        raise ValueError('받는이 관리에서 등록된 업체를 선택하세요.')
                    items = data.get('items', [])
                    if not 1 <= len(items) <= 8:
                        raise ValueError('품목은 전표당 1~8개까지 입력하세요.')
                    for item in items:
                        if not item.get('name','').strip() or not str(item.get('qty','')).isdigit() or not 1 <= int(item['qty']) <= 99999:
                            raise ValueError('품명과 1~99999의 정수 수량을 입력하세요.')
                        month = item.get('productionMonth', '')
                        if month:
                            valid_date(month + '-01')
                        if len(item['name']) > 120:
                            raise ValueError('품명은 120자 이내로 입력하세요.')
                    if old and old['returns']:
                        fields = ('name','serial','qty','fault','productionMonth')
                        if len(items) != len(old['items']) or any(str(a.get(k,'')) != str(b.get(k,'')) for a,b in zip(items,old['items']) for k in fields):
                            raise ValueError('반환 이력이 있는 전표의 품목은 변경할 수 없습니다.')
                        if data['date'] > min(e['date'] for e in old['returns']):
                            raise ValueError('접수일은 반환일 이후로 변경할 수 없습니다.')
                    if data.get('status') not in ('접수','발송','처리중','부분반환','반환완료'):
                        raise ValueError('진행상태가 올바르지 않습니다.')
                    result = dict(old or {})
                    keys = ('date','supplier','phone','company','companyPhone','manager','kind','status','note','shippedDate','trackingNumber','courier')
                    result.update({k: data[k] for k in keys if k in data})
                    result['requesters'] = [name for name in ('GN','대일') if name in requesters]
                    result['items'] = [{k:v for k,v in item.items() if k in ('name','serial','qty','fault','productionMonth')} for item in items]
                    result['returns'] = old['returns'] if old else []
                    result['version'] = old['version'] + 1 if old else 0
                    if not result['returns'] and result['status'] in ('부분반환', '반환완료'):
                        raise ValueError('반환은 목록의 반환 버튼으로 처리하세요.')
                    if old:
                        result['number'] = old['number']
                    else:
                        result['number'] = next_slip_number(c, data['date'])
                        cursor = c.execute('INSERT INTO slips(body) VALUES(?)', ('{}',))
                        id = cursor.lastrowid
                    result = normalize(result)
                result.pop('id', None)
                c.execute('UPDATE slips SET body=? WHERE id=?', (json.dumps(result,ensure_ascii=False), id))
            return self.send(dict(result, id=id))
        except (ValueError,TypeError,KeyError,AttributeError) as e: return self.send({'error':str(e)},status=400)

if __name__=='__main__':
    host = os.environ.get('AS_HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8000'))
    print(f'AS 전표 관리: http://{host}:{port}', flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
