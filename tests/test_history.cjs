// Completed-slip history, live details and real A4 PDF pagination/margin checks.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');

(async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'as-history-'));
  const server=spawn(process.env.PYTHON_COMMAND||'python3',['-u','-c',"import app; s=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); print(s.server_port,flush=True); s.serve_forever()"],{cwd:path.resolve(__dirname,'..'),env:{...process.env,AS_DATA_DIR:temp},stdio:['ignore','pipe','inherit']});
  let browser;
  try{
    const port=await new Promise((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(Error('Startup timeout')),10000);server.on('error',error=>{clearTimeout(timer);reject(error);});server.stdout.on('data',chunk=>{out+=chunk;if(/^\d+\n/.test(out)){clearTimeout(timer);resolve(Number(out.trim()));}});});
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=`http://127.0.0.1:${port}`,errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const post=async(endpoint,data)=>{const response=await page.request.post(base+endpoint,{data});assert.equal(response.status(),200,await response.text());return response.json();};
    await post('/api/suppliers',{name:'이력 업체',phone:'02-1234-5678'});
    const record={date:'2026-10-18',supplier:'이력 업체',phone:'02-1234-5678',company:'우리 업체',companyPhone:'010-1111-2222',manager:'담당자',kind:'AS 발송',status:'발송',requesters:['GN'],courier:'CJ대한통운',trackingNumber:'0000123456789',note:'HISTORY-NOTE <문자>',
      items:[{name:'메인보드 <img src=x onerror=alert(1)>',serial:'000-OUT-A',qty:2,fault:'부팅 불가',productionMonth:'2025-09',qrCodeNumber:'000-QR',note:'품목 메모'},{name:'메모리',serial:'000-OUT-B',qty:1}]};
    let slip=await post('/api/slips',record);
    slip=await post('/api/returns',{id:slip.id,version:slip.version,date:'2026-10-21',result:'교환',mbRepairReport:'FIRST-REPAIR',items:[{index:0,qty:1,receivedSerial:'000-IN-A'}]});
    slip=await post('/api/returns',{id:slip.id,version:slip.version,date:'2026-10-20',result:'수리',mbRepairReport:'SECOND-REPAIR HISTORY-END-BASIC',items:[{index:0,qty:1,receivedSerial:'000-IN-B'},{index:1,qty:1}]});
    await page.goto(base);await page.locator('[data-view=completed]').click();
    await page.locator('#rows [data-detail]').waitFor();
    assert.deepEqual(await page.locator('#rows .row-actions button').allTextContents(),['상세','수정','삭제']);
    assert.equal(await page.locator('#rows [data-print]').count(),0);
    // Change a record behind an already loaded list: Detail must fetch the latest.
    slip.note='LATEST-HISTORY-NOTE';slip=await post('/api/slips',slip);
    await page.locator('#rows [data-detail]').click();await page.locator('#detailDialog').waitFor({state:'visible'});
    const report=await page.locator('#detailPaper').innerText();
    for(const text of ['LATEST-HISTORY-NOTE','000-OUT-A','000-IN-A','000-IN-B','FIRST-REPAIR','SECOND-REPAIR','0000123456789','2025-09','000-QR','품목 메모'])assert.ok(report.includes(text),text);
    assert.equal(await page.locator('#detailPaper img,#detailPaper input,#detailPaper textarea').count(),0);
    const headings=await page.locator('.history-event-heading h3').allTextContents();
    assert.match(headings[0],/2026-10-18.*출고/);assert.match(headings[1],/2026-10-20.*부분입고/);assert.match(headings[2],/2026-10-21.*입고 완료/);
    const before=await (await page.request.get(base+'/api/slips/'+slip.id)).json();
    async function printReport(name,endToken){
      await page.evaluate(()=>{window.print=()=>{window.historyPrintCalls=(window.historyPrintCalls||0)+1;};});
      await page.locator('#historyPrintBtn').click();
      assert.ok(await page.evaluate(()=>window.historyPrintCalls>0));
      assert.equal(await page.locator('#print-root').getAttribute('class'),'history-print');
      const file=path.join(temp,name+'.pdf');await page.pdf({path:file,preferCSSPageSize:true,displayHeaderFooter:false});
      const info=execFileSync('pdfinfo',[file],{encoding:'utf8'}),size=info.match(/Page size:\s+([\d.]+) x ([\d.]+) pts \(A4\)/);
      assert.ok(size && Math.abs(Number(size[1])-595.28)<1 && Math.abs(Number(size[2])-841.89)<1,'A4 page dimensions');
      const text=execFileSync('pdftotext',['-raw',file,'-'],{encoding:'utf8'});assert.ok(text.includes(endToken),endToken);
      const xml=execFileSync('pdftotext',['-bbox',file,'-'],{encoding:'utf8'});
      const pages=[...xml.matchAll(/<page width="([\d.]+)" height="([\d.]+)">([\s\S]*?)<\/page>/g)];assert.ok(pages.length);
      for(const [,w,h,body]of pages){
        for(const word of body.matchAll(/<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)"/g)){
          const [x1,y1,x2,y2]=word.slice(1).map(Number);
          assert.ok(x1>=32&&x2<=Number(w)-32,`${name} text exceeds horizontal A4 margins: ${word[0]}`);
          assert.ok(y1>=32&&y2<=Number(h)-32,`${name} text exceeds vertical A4 margins: ${word[0]}`);
        }
      }
      return pages.length;
    }
    await printReport('basic','HISTORY-END-BASIC');
    assert.deepEqual(await (await page.request.get(base+'/api/slips/'+slip.id)).json(),before,'Viewing/printing must not modify saved history');
    await page.locator('[data-close=detailDialog]').click();
    await page.evaluate(()=>preparePrint());
    assert.equal(await page.locator('#print-root>*').count(),0,'Closing details must discard stale print content');
    const longItems=Array.from({length:8},(_,index)=>({name:('긴 품명 '+index+' ').repeat(15).slice(0,120),serial:'OUT-'+index+'S'.repeat(32),qty:4,productionMonth:'2025-09',qrCodeNumber:'Q'.repeat(500),note:('긴 비고 '+index+' ').repeat(80).slice(0,500),fault:'불량 증상 '.repeat(10).slice(0,70)}));
    let long=await post('/api/slips',{...record,note:'장문 이력',items:longItems});
    for(let event=0;event<4;event++)long=await post('/api/returns',{id:long.id,version:long.version,date:`2026-10-${19+event}`,result:event===3?'교환':'수리',mbRepairReport:('수리 내역 상세 '+event+' ').repeat(100).slice(0,1900)+(event===3?' FINAL-HISTORY-END':' REPAIR-END-'+event),items:longItems.map((_,index)=>({index,qty:1,receivedSerial:'IN-'+event+'-'+index+'X'.repeat(170)}))});
    await page.locator('#refreshList').click();await page.locator(`#rows [data-detail="${long.id}"]`).click();await page.locator('#detailDialog').waitFor({state:'visible'});
    await page.locator('#detailDialog').screenshot({path:path.join(temp,'history-detail.png')});
    const count=await printReport('long','FINAL-HISTORY-END');assert.ok(count>1,'Long reports must paginate rather than clip or shrink to one sheet');
    await page.setViewportSize({width:390,height:844});
    const smallCount=await printReport('narrow','FINAL-HISTORY-END');assert.equal(smallCount,count,'Print layout must be independent of screen width');
    await page.locator('[data-close=detailDialog]').click();
    await page.evaluate(s=>preview(s),slip);await page.evaluate(()=>preparePrint());
    assert.equal(await page.locator('#print-root.history-print').count(),0);
    assert.equal(await page.locator('#print-root .slip').count(),2);
    assert.deepEqual(errors,[]);
    console.log(`PASS: completed actions, fresh read-only history, chronological receipts, serial/repair data, A4 margins, ${count}-page pagination, narrow-screen print and slip-mode switching.`);
  }finally{
    if(browser)await browser.close();server.kill();
    if(process.env.KEEP_PRINT_ARTIFACTS)console.log('History artifacts:',temp);else fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
