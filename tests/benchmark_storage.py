"""Optional realistic-size check: python tests/benchmark_storage.py --records 10000."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import storage


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--records',type=int,default=10000)
    options=parser.parse_args()
    if options.records < 100: parser.error('--records must be at least 100')
    with tempfile.TemporaryDirectory(prefix='as-scale-') as temporary:
        app.DATA=Path(temporary)
        base={'date':'2026-10-19','supplier':'성능검증 업체','company':'보낸 업체','kind':'AS 발송','status':'발송','version':0,'requesters':['GN'],
              'trackingNumber':'0000123456789','items':[{'name':'메인보드 DDR5','serial':'000SERIAL','qrCodeNumber':'000QR-CODE','qty':1,'fault':'부팅 점검'}]}
        start=time.perf_counter()
        with app.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO suppliers(name,phone,address) VALUES('성능검증 업체','','')")
            for i in range(1,options.records+1):
                record=app.normalize(dict(base,number=f'261019-{i:05d}',items=[dict(base['items'][0],serial=f'SERIAL-{i:08d}')]))
                slip_id=c.execute("INSERT INTO slips(body) VALUES('{}')").lastrowid
                storage.save_record(c,slip_id,record)
        seed_seconds=time.perf_counter()-start
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        server.RequestHandlerClass.log_message=lambda *args:None
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        def timed(path,data=None):
            start=time.perf_counter()
            request=urllib.request.Request(origin+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response: content=response.read()
            return (time.perf_counter()-start)*1000,json.loads(content),len(content)
        try:
            pages=[timed('/api/slips/page?view=all&size=50') for _ in range(5)]
            assert pages[-1][1]['total']==options.records and len(pages[-1][1]['rows'])==50
            searches=[timed('/api/slips/page?view=all&q=serial00000042&field=serial') for _ in range(5)]
            assert searches[-1][1]['total']==1
            write_ms,new,_=timed('/api/slips',base)
            new['note']='성능 검증 수정'
            edit_ms,edited,_=timed('/api/slips',new)
            assert edited['version']==1
            with app.db() as c: c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            print(json.dumps({'records':options.records,'seed_seconds':round(seed_seconds,2),
                'page50_median_ms':round(statistics.median(row[0] for row in pages),2),
                'indexed_search_median_ms':round(statistics.median(row[0] for row in searches),2),
                'create_ms':round(write_ms,2),'edit_ms':round(edit_ms,2),'page_response_bytes':pages[-1][2],
                'database_bytes':(app.DATA/'as.db').stat().st_size},ensure_ascii=False,indent=2))
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__=='__main__': main()
