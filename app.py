"""AS 전표 관리 — Python 표준 라이브러리만 사용합니다."""
import io, json, os, re, sqlite3, zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('AS_DATA_DIR', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DEFAULT_RECEIPT_OPTIONS = ('수리', '교환', '정상 판정', '수리 불가', '반송', '기타')

def db():
    c = sqlite3.connect(DATA / 'as.db')
    c.execute('CREATE TABLE IF NOT EXISTS slips (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, phone TEXT NOT NULL, address TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)')
    c.execute('CREATE TABLE IF NOT EXISTS receipt_options (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)')
    c.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)')
    return c

def korea_today():
    return datetime.now(timezone(timedelta(hours=9))).date()


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
    current_date = korea_today()
    # Upgrade old completed slips in memory without losing their original return date.
    if 'returns' not in r:
        r['returns'] = ([{'date': r.get('returned', ''), 'result': r.get('result', ''),
                          'items': [{'index': i, 'qty': int(item['qty'])} for i, item in enumerate(r['items'])]}]
                        if r.get('status') == '반환완료' else [])
    r.setdefault('version', 0)
    r.setdefault('shippedDate', '')  # Legacy shipping date is retained in storage only.
    r.setdefault('trackingNumber', '')
    r.setdefault('courier', '')
    r.setdefault('requesters', [])
    r['shippedMonth'] = str(int(r['date'][5:7])) + '월'
    for i, item in enumerate(r['items']):
        # Preserve old model information as part of the unified product name.
        if item.get('model'):
            item['name'] = ' '.join(filter(None, (item.get('name', ''), item['model'])))
        item.pop('model', None)
        item.setdefault('productionMonth', '')
        item['sourceIndex'] = i
        item['receivedSerial'] = '\n'.join(x.get('receivedSerial', '') for event in r['returns'] for x in event['items'] if x['index'] == i and x.get('receivedSerial'))
        item['returnedQty'] = sum(int(x['qty']) for event in r['returns'] for x in event['items'] if x['index'] == i)
        item['remainingQty'] = int(item['qty']) - item['returnedQty']
        received = [(index, event) for index, event in enumerate(r['returns']) if any(x['index'] == i for x in event['items'])]
        latest = max(received, key=lambda pair: (pair[1]['date'], pair[0]))[1] if received else {}
        item['receiptTreatment'] = latest.get('result', '')
        item['receivedDate'] = latest.get('date', '')
        item_end = datetime.strptime(item['receivedDate'], '%Y-%m-%d').date() if item['remainingQty'] == 0 and item['receivedDate'] else current_date
        item['elapsedDays'] = max(0, (item_end - datetime.strptime(r['date'], '%Y-%m-%d').date()).days)
    if all(i['remainingQty'] == 0 for i in r['items']):
        r['status'] = '반환완료'
    elif r['returns']:
        r['status'] = '부분반환'
    r['returned'] = max((e['date'] for e in r['returns']), default='')
    if r['returns']:
        r['result'] = max(enumerate(r['returns']), key=lambda pair: (pair[1]['date'], pair[0]))[1].get('result', '')
    else:
        r['result'] = ''
    end = datetime.strptime(r['returned'], '%Y-%m-%d').date() if r['status'] == '반환완료' else current_date
    r['elapsedDays'] = max(0, (end - datetime.strptime(r['date'], '%Y-%m-%d').date()).days)
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


def receipt_option_rows(c=None):
    if c is None:
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            return receipt_option_rows(connection)
    # Seed once. An intentionally emptied user list must stay empty.
    if not c.execute("SELECT 1 FROM app_meta WHERE key='receipt_options_seeded'").fetchone():
        c.executemany('INSERT OR IGNORE INTO receipt_options(name) VALUES(?)', [(name,) for name in DEFAULT_RECEIPT_OPTIONS])
        c.execute("INSERT INTO app_meta VALUES('receipt_options_seeded','1')")
    return [dict(id=i, name=name) for i,name in c.execute('SELECT id,name FROM receipt_options ORDER BY id')]


def receipt_treatment(value, previous='', c=None):
    choices = [row['name'] for row in receipt_option_rows(c)]
    if not isinstance(value, str) or (value not in ('', *choices) and value != previous):
        raise ValueError('입고처리는 등록된 목록에서 선택하세요. 목록이 바뀌었다면 새로고침하세요.')
    return value


def clean_received_serial(value):
    if not isinstance(value, str) or len(value) > 400:
        raise ValueError('입고 시리얼은 400자 이내로 입력하세요. 여러 개는 쉼표나 줄바꿈으로 구분하세요.')
    return value.strip()


def update_return_serials(r, data, c=None):
    event_index = data.get('eventIndex')
    if type(event_index) is not int or not 0 <= event_index < len(r['returns']):
        raise ValueError('반환 이력을 선택하세요.')
    event = r['returns'][event_index]
    treatment = receipt_treatment(data.get('result', event.get('result', '')), event.get('result', ''), c)
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


def apply_return(r, data, c=None):
    date = valid_date(data.get('date', ''))
    treatment = receipt_treatment(data.get('result', ''), c=c)
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


def editor_returns(old, data, items, c):
    """Validate editable receipt rows and preserve unchanged partial-return groups."""
    edits = data.get('receiptEdits')
    if edits is None:
        # Older API clients can still edit product details without replacing history.
        mapping = {}
        for index,item in enumerate(items):
            source = item.get('sourceIndex', index)
            if source is not None:
                if type(source) is not int or not 0 <= source < len(old['items']) or source in mapping:
                    raise ValueError('수정 품목의 원본 연결이 올바르지 않습니다.')
                mapping[source] = index
        result = []
        for event in old['returns']:
            entries = []
            for entry in event['items']:
                if entry['index'] not in mapping:
                    raise ValueError('입고된 품목을 삭제하려면 전표 수정 화면에서 입고내역도 함께 수정하세요.')
                entries.append(dict(entry, index=mapping[entry['index']]))
            result.append(dict(event, items=entries))
    else:
        if not isinstance(edits, list) or len(edits) > 1000:
            raise ValueError('입고내역이 올바르지 않습니다.')
        groups = {}
        for order, edit in enumerate(edits):
            index, qty = edit.get('index'), edit.get('qty')
            if type(index) is not int or not 0 <= index < len(items) or type(qty) is not int or qty < 1:
                raise ValueError('입고 품목과 수량을 확인하세요.')
            date = valid_date(edit.get('date', ''))
            previous_index = edit.get('eventIndex')
            previous = ''
            if previous_index is not None:
                if type(previous_index) is not int or not 0 <= previous_index < len(old['returns']):
                    raise ValueError('입고내역의 원본 연결이 올바르지 않습니다.')
                previous = old['returns'][previous_index].get('result', '')
            treatment = receipt_treatment(edit.get('result', ''), previous, c)
            serial = clean_received_serial(edit.get('receivedSerial', ''))
            key = (previous_index if previous_index is not None else f'new-{order}', date, treatment)
            event = groups.setdefault(key, {'date':date, 'result':treatment, 'items':[]})
            if any(entry['index'] == index for entry in event['items']):
                raise ValueError('같은 입고내역의 품목이 중복되었습니다.')
            event['items'].append({'index':index, 'qty':qty, **({'receivedSerial':serial} if serial else {})})
        # Preserve original chronological entry order when receipt cards are grouped by product.
        result = [event for key,event in sorted(groups.items(), key=lambda pair: (isinstance(pair[0][0],str), int(pair[0][0].split('-')[1]) if isinstance(pair[0][0],str) else pair[0][0]))]
    totals = [0] * len(items)
    for event in result:
        if event['date'] < data['date']:
            raise ValueError('입고일은 접수일 (출고일)보다 빠를 수 없습니다.')
        for entry in event['items']:
            totals[entry['index']] += entry['qty']
    if any(total > int(item['qty']) for total,item in zip(totals, items)):
        raise ValueError('입고수량이 출고수량을 초과합니다. 수량 또는 입고내역을 함께 수정하세요.')
    expected = '반환완료' if all(total == int(item['qty']) for total,item in zip(totals, items)) else '부분반환' if any(totals) else None
    if expected and data['status'] != expected:
        raise ValueError(f'입고수량에 맞춰 진행상태를 {expected}(으)로 선택하세요.')
    if not expected and data['status'] in ('부분반환','반환완료'):
        raise ValueError('선택한 진행상태에 맞는 입고내역을 입력하세요.')
    return result


def xlsx(rows):
    headers = ['전표번호', '접수일 (출고일)', '출고월', '택배사', '출고 송장번호',
               '의뢰업체', '받는이', '보낸이', '품명', '생산 년월', '출고 시리얼번호',
               '입고 시리얼번호', '수량', '불량 증상', '구분', '진행상태', '비고',
               '반환수량', '미반환수량', '경과일수', '입고처리', '입고일']
    table = [headers]
    for r in rows:
        for item in r['items']:
            table.append([
                r['number'], r['date'], r.get('shippedMonth', ''),
                r.get('courier', ''), r.get('trackingNumber', ''), ' · '.join(r.get('requesters', [])),
                r['supplier'], r.get('company', ''), item.get('name', ''), item.get('productionMonth', ''),
                item.get('serial', ''), item.get('receivedSerial', ''), item.get('qty', 1),
                item.get('fault', ''), r['kind'], r['status'], r.get('note', ''), item['returnedQty'],
                item['remainingQty'], item['elapsedDays'], item.get('receiptTreatment', ''), item.get('receivedDate', '')])
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
    options = [row['name'] for row in receipt_option_rows()]
    option_cells = ''.join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{escape(name)}</t></is></c></row>'
                           for i,name in enumerate(options or [''], 1))
    option_sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData>' + option_cells + '</sheetData></worksheet>')
    treatment_column = col(len(headers) - 1)
    validation = ''
    if len(table) > 1:
        validation = (f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="1" '
                      f'errorStyle="stop" sqref="{treatment_column}2:{treatment_column}{len(table)}">'
                      '<formula1>ReceiptOptions</formula1></dataValidation></dataValidations>')
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="AS 누적내역" sheetId="1" r:id="rId1"/><sheet name="입고처리목록" sheetId="2" state="hidden" r:id="rId2"/></sheets><definedNames><definedName name="ReceiptOptions">입고처리목록!$A$1:$A$'+str(max(1,len(options)))+'</definedName></definedNames></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet2.xml', option_sheet)
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
        if path=='/api/receipt-options': return self.send([row['name'] for row in receipt_option_rows()])
        if path=='/api/receipt-options/manage': return self.send(receipt_option_rows())
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
            if path in ('/api/receipt-options', '/api/receipt-options/delete'):
                with db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    receipt_option_rows(c)
                    id = data.get('id')
                    if id and not c.execute('SELECT 1 FROM receipt_options WHERE id=?', (id,)).fetchone():
                        raise ValueError('입고처리 항목이 없습니다.')
                    if path.endswith('/delete'):
                        if not id:
                            raise ValueError('삭제할 항목을 선택하세요.')
                        c.execute('DELETE FROM receipt_options WHERE id=?', (id,))
                    else:
                        name = str(data.get('name', '')).strip()
                        if not name or len(name) > 60 or any(ord(ch) < 32 for ch in name):
                            raise ValueError('입고처리 항목을 줄바꿈 없이 1~60자로 입력하세요.')
                        try:
                            if id:
                                c.execute('UPDATE receipt_options SET name=? WHERE id=?', (name, id))
                            else:
                                c.execute('INSERT INTO receipt_options(name) VALUES(?)', (name,))
                        except sqlite3.IntegrityError:
                            raise ValueError('이미 등록된 입고처리 항목입니다.')
                return self.send({'ok': True})
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
                    result = update_return_serials(old, data, c)
                elif path == '/api/returns':
                    if not old:
                        raise ValueError('반환할 전표를 선택하세요.')
                    result = apply_return(old, data, c)
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
                    if data.get('status') not in ('접수','발송','처리중','부분반환','반환완료'):
                        raise ValueError('진행상태가 올바르지 않습니다.')
                    result = dict(old or {})
                    keys = ('date','supplier','phone','company','companyPhone','manager','kind','status','note','shippedDate','trackingNumber','courier')
                    result.update({k: data[k] for k in keys if k in data})
                    result['requesters'] = [name for name in ('GN','대일') if name in requesters]
                    result['items'] = [{k:v for k,v in item.items() if k in ('name','serial','qty','fault','productionMonth')} for item in items]
                    result['returns'] = editor_returns(old, data, items, c) if old else []
                    result['version'] = old['version'] + 1 if old else 0
                    if not result['returns'] and result['status'] in ('부분반환', '반환완료'):
                        raise ValueError('반환은 목록의 반환 버튼으로 처리하세요.')
                    if old:
                        number = str(data.get('number', old['number'])).strip()
                        if not number or len(number) > 60 or any(ord(ch) < 32 for ch in number):
                            raise ValueError('전표번호를 1~60자로 입력하세요.')
                        if any(json.loads(body).get('number') == number for other_id,body in c.execute('SELECT id,body FROM slips WHERE id<>?', (id,))):
                            raise ValueError('이미 사용 중인 전표번호입니다.')
                        result['number'] = number
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
