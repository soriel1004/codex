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
    const page = await browser.newPage({viewport:{width:1600,height:1100}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${port}`);
    assert.equal(await page.locator('[name=address],[name=business],[name=supplierAddress],[data-key=model]').count(),0);
    await page.locator('#settingsBtn').click();
    await page.locator('#settingsForm [name=company]').fill('보낸 업체');
    await page.locator('#settingsForm [name=companyPhone]').fill('010-1111-2222');
    await page.getByRole('button',{name:'설정 저장',exact:true}).click();
    await page.locator('#suppliersBtn').click();
    await page.locator('#supplierForm [name=name]').fill('받는 업체');
    await page.locator('#supplierForm [name=phone]').fill('02-123-4567');
    await page.getByRole('button',{name:'받는이 저장',exact:true}).click();
    await page.locator('#supplierRows tr').filter({hasText:'받는 업체'}).waitFor();
    await page.locator('[data-close=suppliers]').click();
    await page.locator('#newBtn').click();
    await page.locator('#slipForm [name=date]').fill('2026-10-19');
    await page.locator('#slipForm [name=supplier]').selectOption({label:'받는 업체'});
    assert.equal(await page.locator('#slipForm [name=phone]').inputValue(),'02-123-4567');
    await page.locator('#shippedDate').fill('2026-12-31');
    assert.equal(await page.locator('#shippedMonth').innerText(),'12월');
    await page.locator('#shippedDate').fill('');
    assert.equal(await page.locator('#shippedMonth').innerText(),'—월');
    await page.locator('#shippedDate').fill('2026-01-31');
    assert.equal(await page.locator('#shippedMonth').innerText(),'1월');
    await page.locator('#slipForm [name=trackingNumber]').fill('0000123456789');
    await page.locator('#manageProducts').click();
    const productName='삼성 DDR5 <16GB> "메모리"';
    for (const name of [productName,'메인보드']) {
      await page.locator('#productForm [name=name]').fill(name);
      await page.getByRole('button',{name:'품명 저장',exact:true}).click();
      await page.locator('#productRows tr').filter({hasText:name}).waitFor();
    }
    await page.locator('[data-close=products]').click();
    await page.locator('#items tr').first().locator('[data-key=name]').selectOption({label:productName});
    await page.locator('#items tr').first().locator('[data-key=serial]').fill('001234');
    await page.locator('#items tr').first().locator('[data-key=qty]').fill('2');
    await page.locator('#addItem').click();
    await page.locator('#items tr').last().locator('[data-key=name]').selectOption({label:'메인보드'});
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-return]').waitFor();
    assert.match(await page.locator('#rows').innerText(),/0000123456789/);
    assert.equal(await page.locator('#rows .month-badge').innerText(),'1월');
    await page.reload(); await page.locator('#rows [data-edit]').click();
    assert.equal(await page.locator('#shippedDate').inputValue(),'2026-01-31');
    assert.equal(await page.locator('#shippedMonth').innerText(),'1월');
    assert.equal(await page.locator('#items tr').first().locator('[data-key=name]').inputValue(),productName);
    await page.locator('#shippedDate').fill('2026-02-28');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows .month-badge').filter({hasText:'2월'}).waitFor();
    await page.locator('#rows [data-return]').click();
    await page.locator('#returnItems tr').first().locator('[type=checkbox]').check();
    await page.locator('#returnItems tr').first().locator('[type=number]').fill('1');
    await page.locator('#returnSave').click();
    await page.locator('#rows .badge').filter({hasText:'부분반환'}).waitFor();
    await page.locator('#rows [data-edit]').click();
    assert.equal(await page.locator('#items tr').first().locator('select').isDisabled(),true);
    await page.locator('#slipForm [name=note]').fill('부분반환 이력 유지');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-return-all]').waitFor();
    await page.locator('#rows [data-print]').click();
    assert.equal(await page.locator('#paper .slip').count(),2);
    const proof=await page.locator('#paper').innerText();
    assert.ok(proof.includes(productName));
    assert.ok(proof.includes('받는이 보관용')&&proof.includes('보낸이 보관용'));
    for (const hidden of ['2026-02-28','출고일','출고 월','0000123456789','송장번호','모델명','사업자','주소']) assert.ok(!proof.includes(hidden));
    await page.pdf({path:path.join(temp,'workflow.pdf'),preferCSSPageSize:true});
    const text=execFileSync('pdftotext',['-raw',path.join(temp,'workflow.pdf'),'-'],{encoding:'utf8'});
    assert.ok(!text.includes('0000123456789')&&!text.includes('2026-02-28'));
    assert.ok(text.includes('02-123-4567')&&text.includes('010-1111-2222'));
    await page.locator('[data-close=preview]').click();
    await page.locator('#rows [data-return-all]').click();
    await page.locator('[data-view=completed]').click();
    await page.locator('#rows .badge').filter({hasText:'반환완료'}).waitFor();
    await page.locator('#rows [data-return]').click();
    assert.equal(await page.locator('.history-entry').count(),2);
    assert.deepEqual(errors,[]);
    console.log('PASS: receiver/sender phone setup, nested product registration and selection, shipment month changes, persistent tracking number, partial/full returns and private-field exclusion from printed PDF.');
  } finally {
    if(browser) await browser.close();
    server.kill();
    if (process.env.KEEP_PRINT_ARTIFACTS) console.log('Print artifacts:',temp);
    else fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
