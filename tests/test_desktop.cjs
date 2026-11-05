// Local desktop gateway against an actual backend; OS picker replaced by a test path.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'as-desktop-'));
  const target = path.join(temp, '선택한 백업.db');
  const script = "import app,desktop_session,threading,os; from pathlib import Path; s=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler); threading.Thread(target=s.serve_forever,daemon=True).start(); d=desktop_session.DesktopSession('http://127.0.0.1:'+str(s.server_port),on_exit=s.shutdown,picker=lambda name:Path(os.environ['TEST_BACKUP'])); print('URL='+d.start(open_browser=False),flush=True); d.wait(); s.server_close()";
  const child = spawn(process.env.PYTHON_COMMAND || 'python3', ['-u', '-c', script], {
    cwd:path.resolve(__dirname, '..'), env:{...process.env, AS_DATA_DIR:path.join(temp, 'data'), TEST_BACKUP:target}, stdio:['ignore','pipe','inherit']
  });
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Startup timeout')), 10000);
      child.on('error', error => { clearTimeout(timer); reject(error); });
      child.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/URL=(http:\/\/[^\s]+)/);
        if (match) { clearTimeout(timer); resolve(match[1]); }
      });
    });
    browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
    const page = await browser.newPage({viewport:{width:1440, height:1000}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#slipList[aria-busy=false]').waitFor();
    assert.ok((await page.locator('.stats article').first().boundingBox()).height <= 70);
    assert.equal(await page.locator('.stats article').count(), 4);
    assert.equal(await page.locator('.stats p').count(), 0);
    await page.locator('#backupBtn').click();
    await page.locator('#toast').filter({hasText:'백업 파일을 저장했습니다'}).waitFor();
    assert.ok(fs.existsSync(target));
    const integrity = execFileSync(process.env.PYTHON_COMMAND || 'python3', ['-c', "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('PRAGMA quick_check').fetchone()[0])", target], {encoding:'utf8'});
    assert.equal(integrity.trim(), 'ok');
    await page.setViewportSize({width:390, height:844});
    assert.ok((await page.locator('.stats article').first().boundingBox()).height <= 62);
    await page.locator('#exitBtn').click();
    await page.locator('#exitDialog').waitFor({state:'visible'});
    assert.match(await page.locator('#exitMessage').innerText(), /다른 PC의 연결도 끊기며/);
    await page.locator('#exitDialog [data-close=exitDialog]').last().click();
    assert.equal(child.exitCode, null, 'Cancel must keep the program running');
    await page.locator('#exitBtn').click();
    const exited = once(child, 'exit');
    await page.locator('#exitConfirm').click();
    assert.deepEqual(await exited, [0, null]);
    assert.deepEqual(errors, []);
    console.log('PASS: desktop gateway, chosen-path SQLite backup, slim cards, narrow viewport, exit cancel and complete local-server shutdown.');
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.signalCode === null) {
      const stopped = once(child, 'exit'); child.kill(); await stopped;
    }
    fs.rmSync(temp, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
