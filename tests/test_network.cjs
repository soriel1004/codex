// Optional: real multi-browser workflow against a single temporary HTTP server.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');

(async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'as-network-'));
  const server=spawn(process.env.PYTHON_COMMAND||'python3',['-u','-c',
    "import app; s=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); print(s.server_port,flush=True); s.serve_forever()"],
    {cwd:path.resolve(__dirname,'..'),env:{...process.env,AS_DATA_DIR:temp},stdio:['ignore','pipe','inherit']});
  let browser;
  try {
    const port=await new Promise((resolve,reject)=>{
      let output=''; const timer=setTimeout(()=>reject(Error('Startup timeout')),10000);
      server.on('error',error=>{clearTimeout(timer);reject(error);});
      server.stdout.on('data',chunk=>{output+=chunk;if(/^\d+\n/.test(output)){clearTimeout(timer);resolve(Number(output.trim()));}});
    });
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
    const contexts=await Promise.all([browser.newContext(),browser.newContext()]);
    const pages=await Promise.all(contexts.map(context=>context.newPage()));
    const origin=`http://127.0.0.1:${port}`, errors=[], fullListRequests=[];
    for(const page of pages){
      page.on('pageerror',error=>errors.push(error.message));
      page.on('request',request=>{if(new URL(request.url()).pathname==='/api/slips'&&request.method()==='GET')fullListRequests.push(request.url());});
    }
    const post=async(endpoint,data)=>{
      const response=await contexts[0].request.post(origin+endpoint,{data});
      assert.equal(response.status(),200,await response.text()); return response.json();
    };
    await post('/api/suppliers',{name:'공동 사용 업체'});
    await post('/api/settings',{company:'우리 업체'});
    for(let i=1;i<=57;i++) await post('/api/slips',{date:'2026-10-19',supplier:'공동 사용 업체',company:'우리 업체',kind:'AS 발송',status:'발송',items:[{name:'메인보드',serial:`NETWORK-${String(i).padStart(5,'0')}`,qty:1}]});
    const ready=page=>page.locator('#slipList[aria-busy=false]').waitFor();
    for(const page of pages){await page.goto(origin);await ready(page);}
    assert.equal(await pages[0].locator('#rows tr').count(),50);
    assert.equal(await pages[0].locator('#count').innerText(),'57건');
    await pages[0].locator('#nextPage').click();await ready(pages[0]);
    assert.equal(await pages[0].locator('#rows tr').count(),7);
    for(const page of pages){
      await page.locator('#search').fill('network00001');await ready(page);
      assert.equal(await page.locator('#rows tr').count(),1);
      assert.equal(await page.locator('[data-view=all]').getAttribute('class'),'active');
      await page.locator('#rows [data-edit]').click();await page.locator('#editor').waitFor({state:'visible'});
    }
    await pages[0].locator('#slipForm [name=note]').fill('PC 1 저장');
    await pages[1].locator('#slipForm [name=note]').fill('PC 2 미저장 입력 보존');
    await pages[0].getByRole('button',{name:'전표 저장',exact:true}).click();
    await pages[0].locator('#editor').waitFor({state:'hidden'});await ready(pages[0]);
    const conflict=pages[1].waitForResponse(r=>new URL(r.url()).pathname==='/api/slips'&&r.request().method()==='POST');
    await pages[1].getByRole('button',{name:'전표 저장',exact:true}).click();
    assert.equal((await conflict).status(),409);
    assert.equal(await pages[1].locator('#editor').isVisible(),true);
    assert.equal(await pages[1].locator('#slipForm [name=note]').inputValue(),'PC 2 미저장 입력 보존');
    await pages[1].locator('[data-close=editor]').last().click();
    await pages[1].locator('#rows [data-edit]').click();await pages[1].locator('#editor').waitFor({state:'visible'});
    assert.equal(await pages[1].locator('#slipForm [name=note]').inputValue(),'PC 1 저장');
    await pages[1].locator('#slipForm [name=note]').fill('PC 2 최신 내용으로 수정');
    await pages[1].getByRole('button',{name:'전표 저장',exact:true}).click();
    await pages[1].locator('#editor').waitFor({state:'hidden'});await ready(pages[1]);
    await pages[0].locator('#rows [data-edit]').click();await pages[0].locator('#editor').waitFor({state:'visible'});
    assert.equal(await pages[0].locator('#slipForm [name=note]').inputValue(),'PC 2 최신 내용으로 수정');
    await pages[0].locator('#slipForm [name=note]').fill('작업 중인 입력');
    await pages[0].evaluate(()=>refreshWhenIdle());
    assert.equal(await pages[0].locator('#slipForm [name=note]').inputValue(),'작업 중인 입력');
    await pages[0].locator('[data-close=editor]').last().click();
    await pages[0].locator('#resetSearch').click();await ready(pages[0]);
    await pages[0].locator('#pageSize').selectOption('25');await ready(pages[0]);
    assert.equal(await pages[0].locator('#rows tr').count(),25);
    await post('/api/slips',{date:'2026-10-19',supplier:'공동 사용 업체',company:'우리 업체',kind:'AS 발송',status:'발송',items:[{name:'다른 PC 새 등록',serial:'NEW-NETWORK',qty:1}]});
    await pages[0].evaluate(()=>refreshWhenIdle());await ready(pages[0]);
    assert.equal(await pages[0].locator('#count').innerText(),'58건');
    assert.ok((await pages[0].locator('#rows').innerText()).includes('다른 PC 새 등록'));
    // A delayed old query must not replace a newer search result.
    await pages[0].route('**/api/slips/page?*',async route=>{
      if(new URL(route.request().url()).searchParams.get('q')==='slow'){
        await new Promise(resolve=>setTimeout(resolve,700));
        try{await route.fulfill({json:{rows:[],total:0,page:1,pages:1,size:25,stats:{total:58,active:58,completed:0,suppliers:1}}});}catch{}
      }else await route.continue();
    });
    const slow=pages[0].waitForRequest(r=>new URL(r.url()).searchParams.get('q')==='slow');
    await pages[0].locator('#search').fill('slow');await slow;
    await pages[0].locator('#search').fill('newnetwork');await ready(pages[0]);
    assert.equal(await pages[0].locator('#rows tr').count(),1);
    assert.ok((await pages[0].locator('#rows').innerText()).includes('다른 PC 새 등록'));
    assert.deepEqual(fullListRequests,[]);
    assert.deepEqual(errors,[]);
    console.log('PASS: two browsers share one server; page-limited loading, cross-page search, stale-edit protection, fresh details, preserved input, refresh and latest-query results.');
  }finally{
    if(browser)await browser.close();server.kill();fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
