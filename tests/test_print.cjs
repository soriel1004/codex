// Optional print regression check: Playwright, Chromium and Poppler utilities are required.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'as-print-test-'));
  const server = spawn(process.env.PYTHON_COMMAND || 'python3', ['-u', '-c',
    "import app; s=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); print(s.server_port,flush=True); s.serve_forever()"],
    {cwd:path.resolve(__dirname,'..'), env:{...process.env, AS_DATA_DIR:temp}, stdio:['ignore','pipe','inherit']});
  let browser;
  try {
    const port = await new Promise((resolve,reject) => {
      let out = '';
      const timer = setTimeout(() => reject(Error('Server startup timed out')),10000);
      server.on('error', e => {clearTimeout(timer); reject(e);});
      server.on('exit', code => {clearTimeout(timer); reject(Error('Server exited: '+code));});
      server.stdout.on('data', chunk => {
        out += chunk;
        if (/^\d+\n/.test(out)) {clearTimeout(timer); resolve(Number(out.trim()));}
      });
    });
    browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
    const basic = {
      number:'AS-PRINT-TEST', date:'2026-10-19', kind:'AS 발송', status:'반환완료',
      result:'RECEIPT-RESULT-PRIVATE', returned:'2026-10-25',
      supplier:'공급사', company:'내 업체', phone:'010-1234-5678', companyPhone:'02-123-4567',
      shippedDate:'2031-12-24',shippedMonth:'12월',trackingNumber:'0000-TRACKING-PRIVATE',courier:'COURIER-PRIVATE',requesters:['GN','대일'],
      items:[{name:'메모리 DDR5',model:'MODEL-PRIVATE',serial:'001234',productionMonth:'2025-09',receivedSerial:'000NEW-PRINT',qty:2,returnedQty:0,fault:'부팅 불가 LAST-FAULT-END'}],
      note:'TAIL-NOTE-OK'
    };
    const long = {...basic, supplier:'공급사'.repeat(20), company:'컴퓨터'.repeat(20),
      supplierAddress:'SUPPLIER-ADDRESS-PRIVATE', address:'ADDRESS-PRIVATE',
      business:'BUSINESS-PRIVATE', manager:'담당자'.repeat(10), result:'RECEIPT-RESULT-PRIVATE'.repeat(25),
      note:'비고 확인 '.repeat(27)+'TAIL-NOTE-OK',
      items:Array.from({length:8},(_,i)=>({name:(`부품${i} `+'긴품명'.repeat(40)).slice(0,120),
        model:'MODEL-PRIVATE',serial:'SERIAL12'.repeat(5),productionMonth:'2025-09',receivedSerial:'NEW-SERIAL-'.repeat(8),qty:99999,returnedQty:99999,
        fault:('화면 출력 불량 및 재시작 확인 '.repeat(4)).slice(0,56)+(i===7?'LAST-FAULT-END':'')}))
    };
    const unbroken = {...long, items:long.items.map((i,n)=>({...i, fault:'W'.repeat(56)+(n===7?'LAST-FAULT-END':'W'.repeat(13))}))};
    for (const [name, fixture, width] of [['basic',basic,1440],['eight-long',long,1440],['narrow-unbroken',unbroken,390]]) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors=[]; page.on('pageerror', e => errors.push(e.message));
      await page.goto(`http://127.0.0.1:${port}`);
      await page.evaluate(s => preview(s), fixture);
      await page.evaluate(() => document.fonts.ready);
      // Scroll the modal: the physical print origin must remain the page's top left.
      await page.locator('#preview').evaluate(el=>el.scrollTop=500);
      await page.emulateMedia({media:'print'});
      await page.evaluate(()=>window.dispatchEvent(new Event('beforeprint')));
      const layout = await page.locator('#print-root').evaluate(root => {
        const pageBox = root.getBoundingClientRect();
        const copies = [...root.querySelectorAll('.slip')].map(slot => {
          const box=slot.getBoundingClientRect(), content=slot.querySelector('.slip-content').getBoundingClientRect();
          const rects=[...slot.querySelectorAll('*')].map(el=>el.getBoundingClientRect());
          return {bottom:content.bottom, slotBottom:box.bottom, right:Math.max(...rects.map(r=>r.right)),
            left:Math.min(...rects.map(r=>r.left)), maxBottom:Math.max(...rects.map(r=>r.bottom)),
            whiteSpace:getComputedStyle(slot.querySelector('td:last-child')).whiteSpace};
        });
        return {left:pageBox.left,right:pageBox.right,bottom:pageBox.bottom,copies};
      });
      assert.equal(layout.copies.length,2);
      for (const copy of layout.copies) {
        assert.equal(copy.whiteSpace,'normal',`${name}: symptom wrapping`);
        assert.ok(copy.left>=layout.left-1,`${name}: left overflow`);
        assert.ok(copy.right<=layout.right+1,`${name}: right overflow`);
        assert.ok(copy.maxBottom<=copy.slotBottom+1,`${name}: clipped content`);
      }
      const file=path.join(temp,name+'.pdf');
      await page.pdf({path:file,preferCSSPageSize:true,displayHeaderFooter:false});
      const info=execFileSync('pdfinfo',[file],{encoding:'utf8'});
      assert.match(info,/Pages:\s+1\b/,`${name}: must be one page`);
      assert.match(info,/Page size:.*A4/,`${name}: must be A4`);
      const text=execFileSync('pdftotext',['-raw',file,'-'],{encoding:'utf8'}).replace(/\s/g,'');
      assert.equal(text.split('TAIL-NOTE-OK').length-1,2,`${name}: both notes must survive`);
      assert.equal(text.split('LAST-FAULT-END').length-1,2,`${name}: last item's symptom must survive`);
      for (const hidden of ['2031-12-24','12월','0000-TRACKING-PRIVATE','COURIER-PRIVATE','MODEL-PRIVATE','ADDRESS-PRIVATE','BUSINESS-PRIVATE','출고월','송장번호','공급자','공급받는자','생산년월','입고시리얼번호','입고처리','입고일','2025-09','000NEW-PRINT','NEW-SERIAL-','RECEIPT-RESULT-PRIVATE','2026-10-25']) {
        assert.ok(!text.includes(hidden),`${name}: must not print ${hidden}`);
      }
      assert.ok(text.includes('받는이보관용') && text.includes('보낸이보관용'),`${name}: updated party labels`);
      assert.ok(text.includes('010-1234-5678') && text.includes('02-123-4567'),`${name}: phone numbers retained`);
      assert.equal(text.split('접수일(출고일)2026-10-19').length-1,2,`${name}: date label and value on both copies`);
      assert.ok(text.includes('GN')&&text.includes('대일'),`${name}: requesting companies retained`);
      const bbox=execFileSync('pdftotext',['-bbox',file,'-'],{encoding:'utf8'});
      for (const word of bbox.matchAll(/<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)"/g)) {
        assert.ok(+word[1]>=33 && +word[2]>=33 && +word[3]<=562 && +word[4]<=809,`${name}: text outside A4's 12 mm printable margins: ${word[0]}`);
      }
      assert.deepEqual(errors,[]);
      console.log(`PASS ${name}: 1 A4 page, 2 complete copies, no text outside margins`);
      await page.close();
    }
  } finally {
    if(browser) await browser.close();
    server.kill();
    if (process.env.KEEP_PRINT_ARTIFACTS) console.log('Print artifacts:',temp);
    else fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
