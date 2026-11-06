// Actual launcher -> client popup -> process restart -> NAS server takeover.
// Only Windows window opening is replaced; production election/restart code runs.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'as-restart-'));
  const install = path.join(temp, 'NAS'), harness = path.join(temp, 'harness');
  fs.mkdirSync(install); fs.mkdirSync(harness);
  const root = path.resolve(__dirname, '..');
  for (const file of ['app.py','auto_server.py','desktop_session.py','launcher.py','storage.py','excel_report.py','backup_restore.py']) fs.copyFileSync(path.join(root,file),path.join(install,file));
  fs.cpSync(path.join(root,'static'),path.join(install,'static'),{recursive:true});
  fs.writeFileSync(path.join(harness,'sitecustomize.py'), `import os, json, auto_server, desktop_session
class WindowsBranch:
    name = 'nt'
    def __getattr__(self, name): return getattr(os, name)
auto_server.os = WindowsBranch()
def open_test_window(session):
    print('DESKTOP=' + json.dumps({'pid':os.getpid(), 'url':session.url}), flush=True)
desktop_session.DesktopSession.open_window = open_test_window
`);
  const children = [];
  let browser;
  function launch(pc) {
    const child = spawn(process.env.PYTHON_COMMAND || 'python3', ['-u',path.join(install,'launcher.py'),'--auto','--open-browser'], {
      cwd:install, detached:true, env:{...process.env, PYTHONPATH:harness + path.delimiter + install,
        LOCALAPPDATA:path.join(temp,pc), AS_AUTO_PORT:'0', AS_ADVERTISE_HOST:'127.0.0.1'}, stdio:['ignore','pipe','inherit']
    });
    const state = {child, windows:[], output:''}; children.push(state);
    child.stdout.on('data', chunk => {
      state.output += chunk;
      for (const line of state.output.split('\n').slice(0,-1)) {
        if (line.startsWith('DESKTOP=')) {
          const value = JSON.parse(line.slice(8));
          if (!state.windows.some(w => w.pid === value.pid)) state.windows.push(value);
        }
      }
    });
    return state;
  }
  async function nextWindow(state, index = 0) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (state.windows[index]) return state.windows[index];
      await new Promise(resolve => setTimeout(resolve,50));
    }
    throw Error('New program window timeout: ' + state.output);
  }
  function killGroup(state) {
    try { process.kill(-state.child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  try {
    const first = launch('PC1'); await nextWindow(first);
    const client = launch('PC2'), initial = await nextWindow(client);
    browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
    const page = await browser.newPage(); const errors = [];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(initial.url); await page.locator('#slipList[aria-busy=false]').waitFor();
    const origin = new URL(initial.url).origin;
    const oldHealth = await (await page.request.get(origin+'/api/health')).json();
    assert.equal((await (await page.request.get(origin+'/api/program')).json()).server, false);
    await page.request.post(origin+'/api/suppliers',{data:{name:'자동 재실행 업체'}});
    await page.request.post(origin+'/api/slips',{data:{date:'2026-10-19',supplier:'자동 재실행 업체',company:'우리 업체',kind:'AS 발송',status:'발송',items:[{name:'메인보드',serial:'000-RESTART',note:'NAS에서 이어받은 메모',qty:1}]}});
    await page.locator('#refreshList').click(); await page.locator('#rows [data-edit]').click();
    await page.locator('#slipForm [name=note]').fill('복사해 보관할 미저장 내용');
    const firstStopped = once(first.child,'exit'); killGroup(first); await firstStopped;
    await page.locator('#connectionDialog').waitFor({state:'visible',timeout:20000});
    assert.equal(await page.locator('#connectionExit').innerText(),'프로그램 종료');
    assert.equal(await page.locator('#connectionRestart').innerText(),'재실행해서 서버로');
    await page.keyboard.press('Enter');
    assert.equal(client.child.exitCode,null,'Scanner Enter must not trigger shutdown');
    await page.locator('[data-close=connectionDialog]').click();
    assert.equal(await page.locator('#slipForm [name=note]').inputValue(),'복사해 보관할 미저장 내용');
    await page.evaluate(()=>checkServerConnection());
    assert.equal(await page.locator('#connectionDialog').isVisible(),false,'Dismissed prompt must not repeat every poll');
    await page.locator('#editor>.connection-warning button').click();
    await page.locator('#connectionDialog').waitFor({state:'visible'});
    const oldExited = once(client.child,'exit');
    await page.locator('#connectionRestart').click();
    assert.deepEqual(await oldExited,[0,null]);
    const restarted = await nextWindow(client,1);
    assert.notEqual(restarted.pid,initial.pid);
    await page.goto(restarted.url); await page.locator('#slipList[aria-busy=false]').waitFor();
    const newOrigin = new URL(restarted.url).origin;
    assert.equal((await (await page.request.get(newOrigin+'/api/program')).json()).server,true);
    const health = await (await page.request.get(newOrigin+'/api/health')).json();
    assert.equal(health.share_id,oldHealth.share_id);
    assert.notEqual(health.leader_token,oldHealth.leader_token);
    const slips = await (await page.request.get(newOrigin+'/api/slips')).json();
    assert.equal(slips[0].items[0].serial,'000-RESTART');
    assert.equal(slips[0].items[0].note,'NAS에서 이어받은 메모');
    const third = launch('PC3'), thirdWindow = await nextWindow(third);
    const page3 = await browser.newPage(); await page3.goto(thirdWindow.url);
    await page3.locator('#slipList[aria-busy=false]').waitFor();
    killGroup(client);
    await page3.locator('#connectionDialog').waitFor({state:'visible',timeout:20000});
    const exited = once(third.child,'exit'); await page3.locator('#connectionExit').click();
    assert.deepEqual(await exited,[0,null]);
    assert.equal(third.windows.length,1,'Exit must not restart the application');
    assert.deepEqual(errors,[]);
    console.log('PASS: server killed, modal over unsaved editor, scanner-safe focus, dismissal, real process restart, NAS takeover with latest data, and popup Exit.');
  } finally {
    if (browser) await browser.close();
    for (const child of children) killGroup(child);
    fs.rmSync(temp,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
