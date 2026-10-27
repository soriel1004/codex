// Real server loss/restart, with the NAS coordinator and two open browser screens.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'as-connection-'));
  const processes = [];
  let browser;
  async function startServer(pc, port = 0) {
    const script = "import app,auto_server,os; from pathlib import Path; c=auto_server.SharedServer(Path(os.environ['TEST_ROOT'])/'NAS',Path(os.environ['TEST_ROOT'])/os.environ['TEST_PC']); assert c.claim(); c.prepare(); s=app.ThreadingHTTPServer(('127.0.0.1',int(os.environ['TEST_PORT'])),app.Handler); c.activate(); c.ready(s.server_port); print('PORT='+str(s.server_port),flush=True); s.serve_forever()";
    const child = spawn(process.env.PYTHON_COMMAND || 'python3', ['-u', '-c', script], {
      cwd:path.resolve(__dirname, '..'),
      env:{...process.env, TEST_ROOT:temp, TEST_PC:pc, TEST_PORT:String(port), AS_ADVERTISE_HOST:'127.0.0.1'},
      stdio:['ignore','pipe','inherit']
    });
    processes.push(child);
    const bound = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Server startup timeout')), 10000);
      child.on('error', error => { clearTimeout(timer); reject(error); });
      child.on('exit', code => { clearTimeout(timer); reject(Error('Server exited: ' + code)); });
      child.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/PORT=(\d+)/);
        if (match) { clearTimeout(timer); resolve(Number(match[1])); }
      });
    });
    return {child, port:bound};
  }
  try {
    const first = await startServer('PC1');
    const origin = `http://127.0.0.1:${first.port}`;
    browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
    const context = await browser.newContext();
    const idle = await context.newPage(), editor = await context.newPage();
    const errors = [], saves = [];
    for (const page of [idle, editor]) {
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => { if (request.method() === 'POST') saves.push(request.url()); });
    }
    await context.request.post(origin + '/api/suppliers', {data:{name:'연결 시험 업체'}});
    const created = await context.request.post(origin + '/api/slips', {data:{date:'2026-10-19', supplier:'연결 시험 업체', company:'우리 업체', kind:'AS 발송', status:'발송', items:[{name:'메인보드', serial:'001234', qty:1}]}});
    assert.equal(created.status(), 200, await created.text());
    const slip = await created.json();
    for (const page of [idle, editor]) {
      await page.goto(origin);
      await page.locator('#slipList[aria-busy=false]').waitFor();
    }
    await editor.locator('#rows [data-edit]').click();
    await editor.locator('#editor').waitFor({state:'visible'});
    await editor.locator('#slipForm [name=note]').fill('서버가 끊겨도 보존할 입력');

    // A single failed probe must not show an alarm; the next good probe resets it.
    await idle.route('**/api/health', route => route.abort());
    await idle.evaluate(() => checkServerConnection());
    assert.equal(await idle.locator('body>.connection-warning').isVisible(), false);
    await idle.unroute('**/api/health');
    assert.equal(await idle.evaluate(() => checkServerConnection()), true);

    // Kill the actual server; ordinary periodic monitoring must notify idle and modal screens.
    const stopped = once(first.child, 'exit');
    first.child.kill('SIGKILL'); await stopped;
    await idle.locator('body>.connection-warning').waitFor({state:'visible', timeout:20000});
    await editor.locator('#editor>.connection-warning').waitFor({state:'visible', timeout:20000});
    assert.match(await editor.locator('#editor>.connection-warning').innerText(), /start-windows\.bat/);
    assert.equal(await editor.locator('#slipForm [name=note]').inputValue(), '서버가 끊겨도 보존할 입력');

    // A failed save keeps the editor and gives an uncertain-result instruction, without retries.
    await editor.getByRole('button', {name:'전표 저장', exact:true}).click();
    await editor.locator('#toast').filter({hasText:'저장 결과를 확인하지 못했습니다'}).waitFor();
    assert.equal(await editor.locator('#editor').isVisible(), true);
    assert.equal(saves.length, 1);

    // Another PC takes over the canonical NAS data. Same-address recovery keeps unsaved input.
    await startServer('PC2', first.port);
    await editor.locator('#editor>.connection-warning button').click();
    await editor.locator('#editor>.connection-warning').waitFor({state:'hidden'});
    await idle.locator('body>.connection-warning button').click();
    await idle.locator('body>.connection-warning').waitFor({state:'hidden'});
    assert.equal(await editor.locator('#slipForm [name=note]').inputValue(), '서버가 끊겨도 보존할 입력');
    assert.equal(saves.length, 1, 'Recovery must not resend the failed save');
    const restored = await context.request.get(origin + '/api/slips/' + slip.id);
    assert.equal(restored.status(), 200);
    assert.equal((await restored.json()).items[0].serial, '001234');
    await editor.getByRole('button', {name:'전표 저장', exact:true}).click();
    await editor.locator('#editor').waitFor({state:'hidden'});
    assert.equal(saves.length, 2);

    // An unresponsive server also fails within the health deadline, rather than hanging forever.
    await idle.route('**/api/health', () => {});
    const started = Date.now();
    assert.equal(await idle.evaluate(() => checkServerConnection()), false);
    assert.equal(await idle.evaluate(() => checkServerConnection()), false);
    assert.ok(Date.now() - started < 10000);
    await idle.locator('body>.connection-warning').waitFor({state:'visible'});
    await idle.unroute('**/api/health');
    assert.equal(await idle.evaluate(() => checkServerConnection()), true);
    await idle.locator('body>.connection-warning').waitFor({state:'hidden'});
    assert.deepEqual(errors, []);
    console.log('PASS: actual NAS server termination alerts idle/modal clients; unsaved input, uncertain saves, no retries, successor recovery and health timeouts.');
  } finally {
    if (browser) await browser.close();
    for (const child of processes) {
      if (child.exitCode === null && child.signalCode === null) {
        const stopped = once(child, 'exit'); child.kill('SIGKILL'); await stopped;
      }
    }
    fs.rmSync(temp, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
