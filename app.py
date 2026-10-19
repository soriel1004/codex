"""AS 전표 관리 — Python 표준 라이브러리만 사용합니다."""
import io, json, os, sqlite3, zipfile
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('AS_DATA_DIR', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)

def db():
    c = sqlite3.connect(DATA / 'as.db')
    c.execute('CREATE TABLE IF NOT EXISTS slips (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, phone TEXT NOT NULL, address TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)')
    return c

def normalize(r):
    # Upgrade old completed slips in memory without losing their original return date.
    if 'returns' not in r:
        r['returns'] = ([{'date': r.get('returned', ''), 'result': r.get('result', ''),
                          'items': [{'index': i, 'qty': int(item['qty'])} for i, item in enumerate(r['items'])]}]
                        if r.get('status') == '반환완료' else [])
    r.setdefault('version', 0)
    for i, item in enumerate(r['items']):
        item['returnedQty'] = sum(int(x['qty']) for event in r['returns'] for x in event['items'] if x['index'] == i)
        item['remainingQty'] = int(item['qty']) - item['returnedQty']
    if all(i['remainingQty'] == 0 for i in r['items']):
        r['status'] = '반환완료'
    elif r['returns']:
        r['status'] = '부분반환'
    r['returned'] = max((e['date'] for e in r['returns']), default='')
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
        return [dict(id=i, name=n, phone=p, address=a) for i,n,p,a in c.execute('SELECT id,name,phone,address FROM suppliers ORDER BY name')]


def valid_date(value):
    try:
        if datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d') != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('올바른 날짜를 입력하세요. (YYYY-MM-DD)')
    return value


def apply_return(r, data):
    date = valid_date(data.get('date', ''))
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
        seen.add(i); clean.append({'index': i, 'qty': qty})
    r['returns'].append({'date': date, 'result': str(data.get('result', ''))[:100], 'items': clean})
    r['result'] = str(data.get('result', ''))[:100] or r.get('result', '')
    r['version'] += 1
    return normalize(r)


def xlsx(rows):
    headers = ['전표번호','접수일','구분','진행상태','공급사','공급사 연락처','보내는 업체','품목','모델명','시리얼번호','수량','불량 증상','처리 결과','최근 반환일','비고','반환수량','미반환수량','품목별 반환이력']
    table = [headers]
    for r in rows:
        for index, item in enumerate(r['items']):
            table.append([r['number'],r['date'],r['kind'],r['status'],r['supplier'],r.get('phone',''),r.get('company',''), item.get('name',''),item.get('model',''),item.get('serial',''),item.get('qty',1),item.get('fault',''),r.get('result',''),r.get('returned',''),r.get('note',''),item['returnedQty'],item['remainingQty'],' / '.join(e['date']+' '+str(x['qty'])+'개 '+e.get('result','') for e in r['returns'] for x in e['items'] if x['index']==index)])
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
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="AS 누적내역" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="18" width="22" customWidth="1"/></cols><sheetData>'+''.join(xmlrows)+'</sheetData><autoFilter ref="A1:R'+str(len(table))+'"/></worksheet>')
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
        if path=='/api/suppliers': return self.send(suppliers())
        if path=='/api/settings':
            with db() as c: row=c.execute('SELECT body FROM settings WHERE id=1').fetchone()
            return self.send(json.loads(row[0]) if row else {})
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
                with db() as c: c.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(data,ensure_ascii=False),))
                return self.send({'ok':True})
            if path == '/api/suppliers':
                name = str(data.get('name', '')).strip()
                if not name or len(name) > 60:
                    raise ValueError('공급사명을 1~60자로 입력하세요.')
                with db() as c:
                    try:
                        if data.get('id'):
                            if not c.execute('SELECT 1 FROM suppliers WHERE id=?', (data['id'],)).fetchone():
                                raise ValueError('공급사가 없습니다.')
                            c.execute('UPDATE suppliers SET name=?,phone=?,address=? WHERE id=?',
                                      (name, str(data.get('phone',''))[:40], str(data.get('address',''))[:100], data['id']))
                        else:
                            c.execute('INSERT INTO suppliers(name,phone,address) VALUES(?,?,?)',
                                      (name, str(data.get('phone',''))[:40], str(data.get('address',''))[:100]))
                    except sqlite3.IntegrityError:
                        raise ValueError('이미 등록된 공급사명입니다.')
                return self.send({'ok': True})
            if path not in ('/api/slips', '/api/returns'):
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
                if path == '/api/returns':
                    if not old:
                        raise ValueError('반환할 전표를 선택하세요.')
                    result = apply_return(old, data)
                else:
                    if not data.get('supplier','').strip() or not data.get('date'):
                        raise ValueError('공급사와 접수일을 입력하세요.')
                    valid_date(data['date'])
                    if not (old and old['supplier'] == data['supplier']) and not c.execute('SELECT 1 FROM suppliers WHERE name=?', (data['supplier'],)).fetchone():
                        raise ValueError('공급사 관리에서 등록된 공급사를 선택하세요.')
                    items = data.get('items', [])
                    if not 1 <= len(items) <= 8:
                        raise ValueError('품목은 전표당 1~8개까지 입력하세요.')
                    for item in items:
                        if not item.get('name','').strip() or not str(item.get('qty','')).isdigit() or not 1 <= int(item['qty']) <= 99999:
                            raise ValueError('품목명과 1~99999의 정수 수량을 입력하세요.')
                    if old and old['returns']:
                        fields = ('name','model','serial','qty','fault')
                        if len(items) != len(old['items']) or any(str(a.get(k,'')) != str(b.get(k,'')) for a,b in zip(items,old['items']) for k in fields):
                            raise ValueError('반환 이력이 있는 전표의 품목은 변경할 수 없습니다.')
                        if data['date'] > min(e['date'] for e in old['returns']):
                            raise ValueError('접수일은 반환일 이후로 변경할 수 없습니다.')
                    if data.get('status') not in ('접수','발송','처리중','부분반환','반환완료'):
                        raise ValueError('진행상태가 올바르지 않습니다.')
                    result = dict(data)
                    result['returns'] = old['returns'] if old else []
                    result['version'] = old['version'] + 1 if old else 0
                    if not result['returns'] and result['status'] in ('부분반환', '반환완료'):
                        raise ValueError('반환은 목록의 반환 버튼으로 처리하세요.')
                    if old:
                        result['number'] = old['number']
                    else:
                        cursor = c.execute('INSERT INTO slips(body) VALUES(?)', ('{}',))
                        id = cursor.lastrowid
                        result['number'] = f'AS-{data["date"].replace("-", "")}-{id:05d}'
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
