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
    return c

def records():
    with db() as c:
        return [dict(json.loads(body), id=id) for id, body in c.execute('SELECT id, body FROM slips ORDER BY id DESC')]

def xlsx(rows):
    headers = ['전표번호','접수일','구분','진행상태','공급사','공급사 연락처','보내는 업체','품목','모델명','시리얼번호','수량','불량 증상','처리 결과','반환일','비고']
    table = [headers]
    for r in rows:
        for item in r['items']:
            table.append([r['number'],r['date'],r['kind'],r['status'],r['supplier'],r.get('phone',''),r.get('company',''), item.get('name',''),item.get('model',''),item.get('serial',''),item.get('qty',1),item.get('fault',''),r.get('result',''),r.get('returned',''),r.get('note','')])
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
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="15" width="22" customWidth="1"/></cols><sheetData>'+''.join(xmlrows)+'</sheetData><autoFilter ref="A1:O'+str(len(table))+'"/></worksheet>')
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
            if path!='/api/slips': return self.send({'error':'찾을 수 없습니다.'},status=404)
            if not data.get('supplier','').strip() or not data.get('date'): raise ValueError('공급사와 접수일을 입력하세요.')
            if data.get('status') not in ['접수','발송','처리중','반환완료']: raise ValueError('진행상태가 올바르지 않습니다.')
            items=data.get('items',[])
            if not 1<=len(items)<=8: raise ValueError('품목은 전표당 1~8개까지 입력하세요.')
            for item in items:
                if not item.get('name','').strip() or not str(item.get('qty','')).isdigit() or int(item['qty'])<1: raise ValueError('품목명과 1 이상의 정수 수량을 입력하세요.')
            datetime.strptime(data['date'],'%Y-%m-%d')
            if data.get('returned'): datetime.strptime(data['returned'],'%Y-%m-%d')
            if data['status']=='반환완료' and not data.get('returned'): raise ValueError('반환완료 시 반환일을 입력하세요.')
            with db() as c:
                id=data.pop('id',None)
                if id:
                    row=c.execute('SELECT body FROM slips WHERE id=?',(id,)).fetchone()
                    if not row: return self.send({'error':'전표가 없습니다.'},status=404)
                    data['number']=json.loads(row[0])['number']
                    c.execute('UPDATE slips SET body=? WHERE id=?',(json.dumps(data,ensure_ascii=False),id))
                else:
                    cursor=c.execute('INSERT INTO slips(body) VALUES(?)',('{}',)); id=cursor.lastrowid
                    data['number']=f'AS-{data["date"].replace("-", "")}-{id:05d}'
                    c.execute('UPDATE slips SET body=? WHERE id=?',(json.dumps(data,ensure_ascii=False),id))
            return self.send(dict(data,id=id))
        except (ValueError,TypeError,KeyError) as e: return self.send({'error':str(e)},status=400)

if __name__=='__main__':
    print('AS 전표 관리: http://127.0.0.1:8000',flush=True)
    ThreadingHTTPServer((os.environ.get('AS_HOST','127.0.0.1'),int(os.environ.get('PORT','8000'))),Handler).serve_forever()
