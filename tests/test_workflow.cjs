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
    assert.equal(await page.locator('.heading h1').innerText(),'AS 업무');
    assert.ok(!(await page.locator('.heading').innerText()).includes('발송부터 반환까지'));
    await page.locator('#settingsBtn').click();
    await page.locator('#settingsForm [name=company]').fill('보낸 업체');
    await page.locator('#settingsForm [name=companyPhone]').fill('010-1111-2222');
    await page.getByRole('button',{name:'설정 저장',exact:true}).click();
    await page.locator('#suppliersBtn').click();
    await page.locator('#supplierForm [name=name]').fill('받는 업체');
    await page.locator('#supplierForm [name=phone]').fill('02-123-4567');
    await page.getByRole('button',{name:'받는이 저장',exact:true}).click();
    await page.locator('#supplierRows tr').filter({hasText:'받는 업체'}).waitFor();
    await page.locator('#supplierForm [name=name]').fill('추가 업체 ABC');
    await page.locator('#supplierForm [name=phone]').fill('010-9876-5432');
    await page.getByRole('button',{name:'받는이 저장',exact:true}).click();
    await page.locator('#supplierRows tr').filter({hasText:'추가 업체 ABC'}).waitFor();
    await page.locator('#supplierSearch').fill('abc');
    assert.equal(await page.locator('#supplierRows [data-supplier-edit]').count(),1);
    assert.match(await page.locator('#supplierRows').innerText(),/추가 업체 ABC/);
    await page.locator('#supplierSearch').fill('021234567');
    assert.equal(await page.locator('#supplierRows [data-supplier-edit]').count(),1);
    assert.match(await page.locator('#supplierRows').innerText(),/받는 업체/);
    await page.locator('#supplierRows [data-supplier-edit]').click();
    assert.equal(await page.locator('#supplierForm [name=name]').inputValue(),'받는 업체');
    await page.locator('#supplierSearch').fill('없는 업체');
    assert.match(await page.locator('#supplierRows').innerText(),/검색 결과가 없습니다/);
    await page.locator('#supplierSearch').fill('');
    assert.equal(await page.locator('#supplierRows [data-supplier-edit]').count(),2);
    await page.locator('[data-close=suppliers]').click();
    assert.match(await page.locator('#newBtn').innerText(),/전표 작성/);
    assert.ok(!(await page.locator('#newBtn').innerText()).includes('새 전표'));
    await page.locator('#newBtn').click();
    assert.equal(await page.locator('#editorTitle').innerText(),'전표 작성');
    assert.equal(await page.locator('#editorNumberField').isVisible(),false);
    assert.equal(await page.locator('[name=requesters]:checked').count(),0);
    await page.locator('[name=requesters][value=GN]').check();
    await page.locator('[name=requesters][value=대일]').check();
    await page.locator('#slipForm [name=courier]').fill('CJ대한통운');
    await page.locator('#slipForm [name=date]').fill('2026-10-19');
    await page.locator('#slipForm [name=supplier]').selectOption({label:'받는 업체'});
    assert.equal(await page.locator('#slipForm [name=phone]').inputValue(),'02-123-4567');
    await page.locator('#receiptDate').fill('2026-12-31');
    assert.equal(await page.locator('#shippedMonth').innerText(),'12월');
    await page.locator('#receiptDate').fill('');
    assert.equal(await page.locator('#shippedMonth').innerText(),'—월');
    await page.locator('#receiptDate').fill('2026-10-19');
    assert.equal(await page.locator('#shippedMonth').innerText(),'10월');
    await page.locator('#slipForm [name=trackingNumber]').fill('0000123456789');
    await page.locator('#manageProducts').click();
    const productName='삼성 DDR5 <16GB> "메모리"';
    for (const name of [productName,'메인보드']) {
      await page.locator('#productForm [name=name]').fill(name);
      await page.getByRole('button',{name:'품명 저장',exact:true}).click();
      await page.locator('#productRows tr').filter({hasText:name}).waitFor();
    }
    await page.locator('#productSearch').fill('ddr5');
    assert.equal(await page.locator('#productRows [data-product-edit]').count(),1);
    assert.match(await page.locator('#productRows').innerText(),/DDR5/);
    await page.locator('#productRows [data-product-edit]').click();
    assert.equal(await page.locator('#productForm [name=name]').inputValue(),productName);
    await page.locator('#productSearch').fill('없는 품명');
    assert.match(await page.locator('#productRows').innerText(),/검색 결과가 없습니다/);
    await page.locator('#productSearch').fill('');
    assert.equal(await page.locator('#productRows [data-product-edit]').count(),2);
    await page.locator('[data-close=products]').click();
    assert.equal(await page.locator('#productNames option').count(),2);
    assert.equal(await page.locator('#items [data-key=name]').first().getAttribute('list'),'productNames');
    await page.locator('#items tr').first().locator('[data-key=name]').fill(productName);
    await page.locator('#items tr').first().locator('[data-key=serial]').fill('001234');
    assert.equal(await page.locator('#items [data-key=productionMonth]').first().getAttribute('type'),'month');
    await page.locator('#items [data-key=productionMonth]').first().fill('2025-09');
    await page.locator('#items tr').first().locator('[data-key=qty]').fill('2');
    await page.locator('#addItem').click();
    const manualName='수기 입력 GPU <임시 제품>';
    await page.locator('#items tr').last().locator('[data-key=name]').fill(manualName);
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-return]').waitFor();
    assert.match(await page.locator('#rows').innerText(),/0000123456789/);
    assert.match(await page.locator('#rows strong').first().innerText(),/^261019-01$/);
    assert.equal(await page.locator('#rows .month-badge').count(),0);
    assert.deepEqual(await page.locator('.panel thead th').allTextContents(),['번호','받는이','구분','품명','송장번호','반환 / 발송','진행상태','경과일수','입고처리 / 입고일','관리']);
    const actionBoxes=await page.locator('#rows .row-actions button').evaluateAll(buttons=>buttons.map(b=>({top:b.getBoundingClientRect().top,left:b.getBoundingClientRect().left})));
    assert.equal(actionBoxes[0].top,actionBoxes[1].top);
    assert.equal(actionBoxes[2].top,actionBoxes[3].top);
    assert.ok(actionBoxes[2].top>actionBoxes[0].top);
    assert.equal(actionBoxes[0].left,actionBoxes[2].left);
    // Simulated saved slips exercise tracking without contacting any carrier.
    const trackingFixture=await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    const trackingRoute=`http://127.0.0.1:${port}/api/slips`;
    async function showTracking(courier, trackingNumber) {
      await page.unroute(trackingRoute);
      await page.route(trackingRoute,route=>route.fulfill({json:[{...trackingFixture[0],courier,trackingNumber}]}));
      await page.reload();
      await page.locator('#rows [data-edit]').waitFor();
    }
    await page.context().route('https://**/*',route=>route.fulfill({contentType:'text/html',body:'<title>배송 조회 테스트</title>'}));
    const carrierCases=[
      ['cj 대한통운','https://trace.cjlogistics.com/web/detail.jsp?slipno=0000123456789'],
      ['한진','https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillResult.do?mCode=MN038&wblnum=0000123456789'],
      ['롯데택배','https://www.lotteglogis.com/home/reservation/tracking/linkView?InvNo=0000123456789'],
      ['우체국','https://service.epost.go.kr/trace.RetrieveDomRigiTraceList.comm?sid1=0000123456789'],
      ['로젠','https://www.ilogen.com/web/personal/trace/0000123456789']
    ];
    for (const [carrier,url] of carrierCases) {
      await showTracking(carrier,'0000-123 456789');
      const link=page.locator('#rows a.tracking-link');
      assert.equal(await link.getAttribute('href'),url);
      assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
      const popupReady=page.waitForEvent('popup');
      await link.click();
      const popup=await popupReady;
      await popup.waitForLoadState();
      assert.equal(popup.url(),url);
      assert.equal(await popup.evaluate(()=>window.opener),null);
      await popup.close();
    }
    for (const carrier of ['처음 보는 택배사','']) {
      await showTracking(carrier,'0000-123 456789');
      await page.locator('#rows [data-track]').click();
      assert.equal(await page.locator('#trackingNumber').inputValue(),'0000-123 456789');
      assert.equal(await page.locator('#trackingNumber').getAttribute('readonly'),'');
      assert.equal(await page.locator('#trackingOpen').isVisible(),false);
      await page.locator('#trackingCarrier').selectOption('한진택배');
      assert.equal(await page.locator('#trackingOpen').getAttribute('href'),carrierCases[1][1]);
      await page.locator('#trackingCarrier').selectOption('');
      assert.equal(await page.locator('#trackingOpen').getAttribute('href'),null);
      await page.locator('[data-close=trackingDialog]').last().click();
    }
    await showTracking('CJ대한통운','');
    assert.equal(await page.locator('#rows .tracking-link').isDisabled(),true);
    assert.match(await page.locator('#rows .tracking-link').innerText(),/송장번호 미등록/);
    await showTracking('CJ대한통운','0000123&other=456');
    assert.equal(await page.locator('#rows a.tracking-link').count(),0);
    await page.locator('#rows [data-track]').click();
    assert.match(await page.locator('#toast').innerText(),/전표 수정에서 확인/);
    assert.equal(await page.locator('#trackingDialog').isVisible(),false);
    await page.unroute(trackingRoute);
    await page.reload(); await page.locator('#rows [data-edit]').waitFor();
    const trackingUnchanged=await (await page.request.get(trackingRoute)).json();
    assert.equal(trackingUnchanged[0].courier,'CJ대한통운');
    assert.equal(trackingUnchanged[0].trackingNumber,'0000123456789');
    await page.locator('#requesterFilter').selectOption('none');
    assert.equal(await page.locator('#rows [data-edit]').count(),0);
    await page.locator('#requesterFilter').selectOption('GN');
    assert.equal(await page.locator('#rows [data-edit]').count(),1);
    await page.locator('#requesterFilter').selectOption('대일');
    assert.equal(await page.locator('#rows [data-edit]').count(),1);
    await page.reload(); await page.locator('#rows [data-edit]').click();
    assert.equal(await page.locator('#receiptDate').inputValue(),'2026-10-19');
    assert.equal(await page.locator('[name=requesters]:checked').count(),2);
    assert.equal(await page.locator('#slipForm [name=courier]').inputValue(),'CJ대한통운');
    assert.equal(await page.locator('#items [data-key=productionMonth]').first().inputValue(),'2025-09');
    assert.equal(await page.locator('#shippedMonth').innerText(),'10월');
    assert.equal(await page.locator('#items tr').first().locator('[data-key=name]').inputValue(),productName);
    assert.equal(await page.locator('#items tr').last().locator('[data-key=name]').inputValue(),manualName);
    await page.locator('#receiptDate').fill('2026-10-18');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows td:first-child small').filter({hasText:'2026-10-18'}).waitFor();
    await page.locator('#rows [data-return]').click();
    assert.equal(await page.locator('#returnItems tr').first().locator('textarea').isEnabled(),true);
    assert.equal(await page.locator('#returnForm [name=result]').evaluate(el=>el.tagName),'SELECT');
    const atBottom = await page.locator('#receiptFields').evaluate(el => el.previousElementSibling.id === 'returnHistory' && el.nextElementSibling.classList.contains('modal-footer'));
    assert.ok(atBottom,'receipt treatment controls must be at the bottom above Save');
    await page.locator('#manageReceiptOptions').click();
    const customTreatment='유상, 교환 "완료"';
    await page.locator('#receiptOptionForm [name=name]').fill(customTreatment);
    await page.getByRole('button',{name:'항목 저장',exact:true}).click();
    await page.locator('#receiptOptionRows tr').filter({hasText:customTreatment}).waitFor();
    await page.locator('#receiptOptionForm [name=name]').fill('삭제용 임시 항목');
    await page.getByRole('button',{name:'항목 저장',exact:true}).click();
    const temporary = page.locator('#receiptOptionRows tr').filter({hasText:'삭제용 임시 항목'});
    await temporary.waitFor(); await temporary.locator('[data-receipt-option-delete]').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/receipt-options/manage'));
    assert.equal(await page.locator('#receiptOptionRows tr').filter({hasText:'삭제용 임시 항목'}).count(),0);
    await page.locator('[data-close=receiptOptionsManager]').click();
    await page.locator('#returnForm [name=result]').selectOption(customTreatment);
    await page.locator('#returnItems tr').first().locator('textarea').fill('000NEW-A');
    assert.equal(await page.locator('#returnItems tr').first().locator('[type=checkbox]').isChecked(),true);
    await page.locator('#returnItems tr').first().locator('[type=number]').fill('1');
    await page.locator('#returnSave').click();
    await page.locator('#rows .badge').filter({hasText:'부분반환'}).waitFor();
    assert.equal(await page.locator('#rows .serial-changes').count(),0);
    const partialData = await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.equal(partialData[0].returns[0].result, customTreatment);
    await page.locator('#rows [data-edit]').click();
    assert.equal(await page.locator('#items tr').first().locator('[data-key=name]').getAttribute('readonly'),null);
    await page.locator('#slipForm [name=note]').fill('부분반환 이력 유지');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-return-all]').waitFor();
    await page.locator('#rows [data-print]').click();
    assert.equal(await page.locator('#paper .slip').count(),2);
    const proof=await page.locator('#paper').innerText();
    assert.ok(proof.includes(productName));
    assert.ok(proof.includes(manualName));
    assert.ok(proof.includes('001234'));
    assert.ok(proof.includes('접수일 (출고일) 2026-10-18'));
    assert.ok(!proof.includes('CJ대한통운'));
    assert.ok(proof.includes('받는이 보관용')&&proof.includes('보낸이 보관용'));
    assert.deepEqual(await page.locator('#paper .print-table').first().locator('th').allTextContents(),['품명','출고 시리얼번호','수량','불량 증상']);
    for (const hidden of ['2026-02-28','출고 월','0000123456789','송장번호','모델명','사업자','주소','생산 년월','입고 시리얼번호','입고처리','입고일','2025-09','000NEW-A',customTreatment]) assert.ok(!proof.includes(hidden));
    await page.pdf({path:path.join(temp,'workflow.pdf'),preferCSSPageSize:true});
    const text=execFileSync('pdftotext',['-raw',path.join(temp,'workflow.pdf'),'-'],{encoding:'utf8'});
    assert.ok(!text.includes('0000123456789')&&!text.includes('2026-02-28'));
    assert.ok(text.includes('02-123-4567')&&text.includes('010-1111-2222'));
    await page.locator('[data-close=preview]').click();
    // Existing receipt information can still be corrected while a slip is in progress.
    await page.locator('#rows [data-return]').click();
    assert.equal(await page.locator('.serial-history').count(),1);
    const lastField = await page.locator('.serial-history').first().locator('[data-history-result]').evaluate(el => Boolean(el.closest('.serial-history').lastElementChild.contains(el)));
    assert.ok(lastField, 'history treatment selector must follow serial fields');
    await page.locator('.serial-history').first().locator('textarea').fill('000CORRECTED');
    await page.locator('.serial-history').first().locator('[data-history-result]').selectOption('정상 판정');
    await page.locator('.serial-history').first().locator('[data-save-serials]').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/slips'));
    const receiptData=await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.equal(receiptData[0].returns[0].result,'정상 판정');
    await page.locator('[data-close=returnDialog]').last().click();
    await page.locator('#rows [data-return-all]').click();
    assert.match(await page.locator('#returnTitle').innerText(),/전량 반환/);
    assert.equal(await page.locator('#returnItems [type=checkbox]:checked').count(),2);
    assert.equal(await page.locator('#returnSave').innerText(),'전량 반환 저장');
    let current=await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.equal(current[0].status,'부분반환');
    assert.equal(current[0].returns.length,1);
    await page.locator('[data-close=returnDialog]').last().click();
    current=await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.equal(current[0].returns.length,1);
    await page.locator('#rows [data-return-all]').click();
    await page.setViewportSize({width:390,height:844});
    const incoming=page.locator('#returnItems tr').first().locator('textarea');
    await incoming.scrollIntoViewIfNeeded();
    const rect=await incoming.boundingBox();
    assert.ok(rect.x>=0&&rect.x+rect.width<=390,'incoming serial must fit the visible phone width');
    await incoming.fill('000FULL-A');
    await page.locator('#returnItems tr').last().locator('textarea').fill('000FULL-B');
    assert.ok((await page.locator('#returnForm [name=result] option').allTextContents()).includes(customTreatment));
    await page.locator('#returnForm [name=result]').selectOption('수리');
    await page.locator('#returnSave').click();
    await page.setViewportSize({width:1600,height:1100});
    await page.locator('[data-view=completed]').click();
    await page.locator('#rows .badge').filter({hasText:'반환완료'}).waitFor();
    const serialChanges=page.locator('#rows .serial-changes');
    assert.equal(await serialChanges.locator('summary').innerText(),'시리얼 변경 · 1품목');
    await serialChanges.locator('summary').click();
    assert.equal(await serialChanges.locator('.serial-change-item').count(),1);
    assert.match(await serialChanges.innerText(),/출고: 001234/);
    assert.match(await serialChanges.innerText(),/입고: 000CORRECTED\n000FULL-A/);
    assert.ok((await serialChanges.innerText()).includes(productName));
    assert.equal(await page.locator('#rows [data-return],#rows [data-return-all]').count(),0);
    assert.equal(await page.locator('#rows .tracking-link').count(),0);
    assert.deepEqual(await page.locator('#rows .row-actions button').allTextContents(),['수정','전표 출력']);
    await page.locator('#rows [data-edit]').click();
    const summaries = page.locator('#editorReceiptSerialRows .editor-receipt-row');
    assert.equal(await summaries.count(),2);
    assert.equal(await page.locator('#items tr').first().locator('[data-key=serial]').inputValue(),'001234');
    assert.equal(await summaries.first().locator('textarea').first().inputValue(),'000CORRECTED');
    assert.equal(await summaries.first().locator('textarea').last().inputValue(),'000FULL-A');
    assert.equal(await summaries.last().locator('textarea').inputValue(),'000FULL-B');
    assert.equal(await summaries.first().locator('textarea').first().getAttribute('readonly'),null);
    assert.equal(await summaries.first().locator('textarea').first().evaluate(el=>getComputedStyle(el).resize),'none');
    assert.equal(await summaries.first().locator('textarea').first().evaluate(el=>el.getBoundingClientRect().height),84);
    const beforeEdit = await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    await page.locator('#slipForm [name=note]').fill('완료 전표 수정 후 입고 시리얼 유지');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-edit]').waitFor();
    const afterEdit = await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.deepEqual(afterEdit[0].returns,beforeEdit[0].returns);
    assert.equal(afterEdit[0].items[0].receivedSerial,'000CORRECTED\n000FULL-A');
    await page.locator('#rows [data-edit]').click();
    await page.locator('#slipForm [name=number]').fill('261019-99');
    await page.locator('#slipForm [name=company]').fill('수정 보낸이');
    await page.locator('#slipForm [name=companyPhone]').fill('010-7777-8888');
    await page.locator('#receiptDate').fill('2026-10-17');
    await page.locator('#items tr').first().locator('[data-key=name]').fill('수정 품명');
    await page.locator('#items tr').first().locator('[data-key=serial]').fill('OUT-EDIT');
    await page.locator('#items tr').first().locator('[data-key=productionMonth]').fill('2025-10');
    await page.locator('#items tr').first().locator('[data-key=qty]').fill('3');
    const firstEntry = page.locator('.editor-receipt-row').first().locator('.editor-receipt-entry').first();
    await firstEntry.locator('[data-receipt-field=receivedSerial]').fill('EDITED-NEW');
    await firstEntry.locator('[data-receipt-field=date]').fill('2026-10-20');
    await firstEntry.locator('[data-receipt-field=qty]').fill('2');
    await firstEntry.locator('[data-receipt-field=result]').selectOption('수리');
    assert.equal(await page.locator('#slipForm [name=status]').inputValue(),'반환완료');
    await page.locator('#addItem').click();
    assert.equal(await page.locator('#items tr').count(),3);
    await page.locator('#items tr').last().locator('.remove').click();
    assert.equal(await page.locator('#items tr').count(),2);
    assert.equal(await page.locator('#slipForm [name=status]').inputValue(),'반환완료');
    await page.getByRole('button',{name:'전표 저장',exact:true}).click();
    await page.locator('#rows [data-edit]').waitFor();
    const fullyEdited=await (await page.request.get(`http://127.0.0.1:${port}/api/slips`)).json();
    assert.equal(fullyEdited[0].number,'261019-99');
    assert.equal(fullyEdited[0].company,'수정 보낸이');
    assert.equal(fullyEdited[0].items[0].name,'수정 품명');
    assert.equal(fullyEdited[0].items[0].serial,'OUT-EDIT');
    assert.equal(fullyEdited[0].items[0].receivedSerial,'EDITED-NEW\n000FULL-A');
    assert.equal(fullyEdited[0].elapsedDays,3);
    await page.locator('#rows .serial-changes').filter({hasText:'EDITED-NEW'}).waitFor();
    await page.locator('#rows [data-print]').click();
    assert.ok(!(await page.locator('#paper').innerText()).includes('EDITED-NEW'));
    assert.match(await page.locator('#paper').innerText(),/OUT-EDIT/);
    await page.locator('[data-close=preview]').click();
    // Completed-list comparison handles unchanged batches, missing numbers,
    // changes on later items, and HTML-like serial text without changing records.
    const serialCases=[
      {items:[{serial:'OLD-1, OLD-2',receivedSerial:' OLD-2\nOLD-1\nOLD-1 '},{serial:'000123',receivedSerial:''}],count:0},
      {items:[{serial:'',receivedSerial:'NEW-ONLY'},{serial:'SAME',receivedSerial:' SAME '}],count:0},
      {items:[{serial:'OLD-1, OLD-2',receivedSerial:'OLD-2\nOLD-1'},{name:'교환 <img src=x onerror=alert(1)>',serial:'000123',receivedSerial:'000124<NEW>'}],count:1},
      {items:[{serial:'OLD-1',receivedSerial:'NEW-1'},{serial:'OLD-2',receivedSerial:'NEW-2'}],count:2}
    ];
    for (const scenario of serialCases) {
      const fixture={...fullyEdited[0],items:fullyEdited[0].items.map((item,index)=>({...item,...scenario.items[index]}))};
      await page.route(trackingRoute,route=>route.fulfill({json:[fixture]}));
      await page.reload();
      await page.locator('[data-view=completed]').click();
      await page.locator('#rows [data-edit]').waitFor();
      assert.equal(await page.locator('#rows .serial-change-item').count(),scenario.count);
      if (scenario.count) {
        assert.equal(await page.locator('#rows .serial-changes summary').innerText(),`시리얼 변경 · ${scenario.count}품목`);
        await page.locator('#rows .serial-changes summary').click();
        assert.ok((await page.locator('#rows .serial-changes').innerText()).includes(scenario.items[1].receivedSerial));
        assert.equal(await page.locator('#rows .serial-changes img').count(),0);
        await page.locator('[data-view=all]').click();
        assert.equal(await page.locator('#rows .serial-change-item').count(),scenario.count);
      }
      await page.unroute(trackingRoute);
    }
    await page.reload();
    await page.locator('#newBtn').click();
    assert.equal(await page.locator('[name=requesters]:checked').count(),0);
    assert.equal(await page.locator('#editorReceiptSerials').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('PASS: completed serial change indicators, unchanged/missing/reordered serials, multiple products and escaped text; five-carrier tracking, completed-slip editing, returns, elapsed days and print.');
  } finally {
    if(browser) await browser.close();
    server.kill();
    if (process.env.KEEP_PRINT_ARTIFACTS) console.log('Print artifacts:',temp);
    else fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
