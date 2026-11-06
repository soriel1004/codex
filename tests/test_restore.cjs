// Backup in one installation, restore through the desktop gateway in a fresh one.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(),'as-restore-ui-'));
  const backup = path.join(temp,'보관한 AS 자료.db'), children = [];
  let browser;
  async function start(name) {
    const script = "import app,desktop_session,threading,os; from pathlib import Path; s=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); threading.Thread(target=s.serve_forever,daemon=True).start(); d=desktop_session.DesktopSession('http://127.0.0.1:'+str(s.server_port),on_exit=s.shutdown,picker=lambda name:Path(os.environ['TEST_BACKUP'])); print('URL='+d.start(open_browser=False),flush=True); d.wait(); s.server_close()";
    const child = spawn(process.env.PYTHON_COMMAND || 'python3',['-u','-c',script],{cwd:path.resolve(__dirname,'..'),env:{...process.env,AS_DATA_DIR:path.join(temp,name),TEST_BACKUP:backup},stdio:['ignore','pipe','inherit']});
    children.push(child);
    const url = await new Promise((resolve,reject) => {
      let out=''; const timer=setTimeout(()=>reject(Error('Startup timeout')),10000);
      child.on('error',error=>{clearTimeout(timer);reject(error);});
      child.stdout.on('data',chunk=>{out+=chunk;const match=out.match(/URL=(http:\/\/[^\s]+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
    });
    return {url,origin:new URL(url).origin,child};
  }
  try {
    const old = await start('old-install');
    browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
    const page = await browser.newPage(); const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(old.url);await page.locator('#slipList[aria-busy=false]').waitFor();
    const post=async(base,endpoint,data)=>{const response=await page.request.post(base+endpoint,{data});assert.equal(response.status(),200,await response.text());return response.json();};
    await post(old.origin,'/api/suppliers',{name:'복원할 받는이',phone:'02-1234-5678'});
    await post(old.origin,'/api/settings',{company:'복원할 보낸이',companyPhone:'010-1111-2222',manager:'담당자'});
    await post(old.origin,'/api/products',{name:'등록된 메인보드'});
    await post(old.origin,'/api/receipt-options',{name:'직접 등록한 입고처리'});
    const record={date:'2026-10-19',supplier:'복원할 받는이',company:'복원할 보낸이',phone:'02-1234-5678',kind:'AS 발송',status:'발송',requesters:['GN'],note:'전표 비고 보존',items:[{name:'메인보드',serial:'000-OUT',qrCodeNumber:'000-QR',productionMonth:'2025-09',note:'품목 비고 보존',qty:2}]};
    const slip=await post(old.origin,'/api/slips',record);
    await post(old.origin,'/api/returns',{id:slip.id,version:slip.version,date:'2026-10-20',result:'직접 등록한 입고처리',mbRepairReport:'수리 내용 보존',items:[{index:0,qty:1,receivedSerial:'000-IN'}]});
    for(let i=0;i<10;i++) await post(old.origin,'/api/slips',{...record,items:Array.from({length:8},(_,j)=>({name:`자료 보존 ${i}-${j}`,serial:`SERIAL-${i}-${j}`,note:('품목메모'+i+j).repeat(65).slice(0,500),qrCodeNumber:('QR'+i+j).repeat(120).slice(0,500),qty:1}))});
    await page.locator('#backupBtn').click();await page.locator('#saveBackupBtn').click();
    await page.locator('#backupMessage').filter({hasText:'저장했습니다'}).waitFor();
    assert.ok(fs.statSync(backup).size>200000,'Exercise streaming beyond the JSON request limit');
    await page.close();
    const fresh=await start('fresh-install'), restoredPage=await browser.newPage();
    restoredPage.on('pageerror',error=>errors.push(error.message));
    await restoredPage.goto(fresh.url);await restoredPage.locator('#slipList[aria-busy=false]').waitFor();
    assert.equal(await restoredPage.locator('#count').innerText(),'0건');
    await restoredPage.locator('#backupBtn').click();
    await restoredPage.locator('#restoreFile').setInputFiles(backup);
    await restoredPage.locator('#restoreMessage').filter({hasText:'검사가 완료'}).waitFor();
    assert.match(await restoredPage.locator('#restoreSummary').innerText(),/전표 11건/);
    assert.equal(await restoredPage.locator('#restoreConfirm').isDisabled(),true);
    assert.equal((await (await restoredPage.request.get(fresh.origin+'/api/slips')).json()).length,0,'Preview must not restore');
    await restoredPage.locator('#backupDialog').screenshot({path:path.join(temp,'restore-preview.png')});
    await restoredPage.locator('#restoreAgree').check();await restoredPage.locator('#restoreConfirm').click();
    await restoredPage.locator('#restoreMessage').filter({hasText:'복원 완료'}).waitFor();
    const saved=await (await restoredPage.request.get(fresh.origin+'/api/slips')).json();assert.equal(saved.length,11);
    const partial=saved.find(row=>row.id===slip.id);
    assert.equal(partial.items[0].receivedSerial,'000-IN');assert.equal(partial.items[0].note,'품목 비고 보존');assert.equal(partial.items[0].mbRepairReport,'수리 내용 보존');
    assert.equal((await (await restoredPage.request.get(fresh.origin+'/api/settings')).json()).company,'복원할 보낸이');
    assert.equal(await restoredPage.locator('#restoreSafetyDownload').isVisible(),true);
    const recovery=await restoredPage.request.get(fresh.origin+await restoredPage.locator('#restoreSafetyDownload').getAttribute('href'));assert.equal(recovery.status(),200);
    assert.equal((await recovery.body()).subarray(0,16).toString(),'SQLite format 3\x00');
    await restoredPage.locator('#restoreFile').setInputFiles({name:'잘못된 백업.db',mimeType:'application/octet-stream',buffer:Buffer.from('not a SQLite backup file')});
    await restoredPage.locator('#restoreMessage').filter({hasText:'AS 데이터베이스 백업'}).waitFor();
    assert.equal(await restoredPage.locator('#restoreConfirm').isDisabled(),true);
    assert.equal((await (await restoredPage.request.get(fresh.origin+'/api/slips')).json()).length,11);
    await restoredPage.locator('#restoreFile').setInputFiles(backup);await restoredPage.locator('#restoreMessage').filter({hasText:'검사가 완료'}).waitFor();
    const changed=await restoredPage.request.post(fresh.origin+'/api/slips',{data:{...record,note:'다른 PC에서 새 전표'}});assert.equal(changed.status(),200);
    await restoredPage.locator('#restoreAgree').check();await restoredPage.locator('#restoreConfirm').click();
    await restoredPage.locator('#restoreMessage').filter({hasText:'현재 자료가 변경'}).waitFor();
    assert.equal((await (await restoredPage.request.get(fresh.origin+'/api/slips')).json()).length,12);
    assert.deepEqual(errors,[]);
    console.log('PASS: saved backup >200 KB, fresh-install restore through local gateway, confirmation, full data/settings, safety download, invalid file and concurrent change protection.');
  } finally {
    if(browser)await browser.close();
    for(const child of children)if(child.exitCode===null&&child.signalCode===null){const stopped=once(child,'exit');child.kill();await stopped;}
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
