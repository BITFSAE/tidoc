"""Update progress and actions, executing the real frontend in Node."""

from tests.test_selection_performance import run_selection


def test_connecting_and_unknown_size_do_not_show_a_fake_zero_percent():
    run_selection(r"""
const connecting = {state: 'downloading', stage: 'starting', downloaded_bytes: 0, total_bytes: 100};
assert.equal(coreUpdateStage(connecting).short, '连接');
assert.match(coreUpdateProgressMarkup(connecting), /正在连接下载服务器/);
assert.doesNotMatch(coreUpdateProgressMarkup(connecting), /aria-valuenow|0%/);
const unknown = {...connecting, stage: 'download', downloaded_bytes: 40, total_bytes: 0};
assert.equal(coreUpdateStage(unknown).short, '下载');
assert.match(coreUpdateProgressMarkup(unknown), /已下载/);
assert.doesNotMatch(coreUpdateProgressMarkup(unknown), /aria-valuenow|0%/);
const known = {...unknown, total_bytes: 100, progress: 0.4};
assert.equal(coreUpdateStage(known).short, '40%');
assert.match(coreUpdateProgressMarkup(known), /aria-valuenow="40"/);
for (const stage of ['verify', 'extract']) {
  const html = coreUpdateProgressMarkup({...connecting, stage});
  assert.match(html, stage === 'verify' ? /校验/ : /准备/);
  assert.doesNotMatch(html, /aria-valuenow|0%/);
}
""")


def test_dialog_and_topbar_follow_the_same_runtime_and_restart_is_explicit():
    run_selection(r"""
(async () => {
  const badge = makeNode(), action = makeNode(), progress = makeNode();
  const topbar = makeNode(), row = makeNode();
  row.querySelector = selector => ({
    '[data-core-update-state]': badge,
    '.update-component-action': action,
    '[data-core-update-progress]': progress,
  }[selector]);
  document.querySelector = selector => selector === '#modalRoot [data-core-update]' ? row
    : selector === '#updateActionBtn' ? topbar : null;
  State.updateStatus = {updates: [{component: 'core', available: true, latest_version: '9.9.9', asset: {auto_update: {size: 100}}}]};
  let installs = 0, finishInstall;
  globalThis.Api = {installCoreUpdate: () => {installs++; return new Promise(resolve => {finishInstall = resolve;});}};
  for (const [stage, done, pct, expected] of [
    ['download', 0, 0, '连接'], ['download', 40, 0.4, '40%'],
    ['verify', 100, 1, '校验'], ['extract', 100, 1, '准备'],
  ]) {
    State.coreUpdateRuntime = {state: 'downloading', stage, downloaded_bytes: done,
      total_bytes: 100, progress: pct, speed_bps: 20, download_mode: 'segmented',
      version: '9.9.9', install_supported: true};
    renderCoreUpdateAction();
    assert.match(badge.innerHTML, new RegExp(expected));
    assert.equal(topbar.querySelector('[data-update-percent]').textContent, expected);
    assert.match(action.innerHTML, /disabled/);
    assert.match(progress.innerHTML, /progressbar/);
    if (stage !== 'download') assert.doesNotMatch(topbar.getAttribute('aria-label'), /秒|加速下载/);
  }
  State.coreUpdateRuntime = {...State.coreUpdateRuntime, state: 'ready', stage: 'ready'};
  renderCoreUpdateAction();
  assert.match(action.innerHTML, /重启更新/);
  assert.equal(progress.innerHTML, '');
  assert.equal(installs, 0);
  const installing = action.querySelector('button').onclick();
  assert.equal(installs, 1);
  assert.match(action.innerHTML, /disabled/);
  assert.match(progress.innerHTML, /正在退出并更新/);
  finishInstall({...State.coreUpdateRuntime});
  await installing;
  State.coreUpdateRuntime = {state: 'failed', error: '<下载中断>'};
  renderCoreUpdateAction();
  assert.match(action.innerHTML, /重试下载/);
  assert.match(progress.innerHTML, /&lt;下载中断&gt;/);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_update_polling_waits_for_each_response_and_stops_when_ready():
    run_selection(r"""
(async () => {
  const timers = [], responses = [], notices = [];
  globalThis.setTimeout = callback => {timers.push(callback); return timers.length;};
  globalThis.Api = {coreUpdateStatus: () => new Promise(resolve => responses.push(resolve))};
  renderCoreUpdateAction = () => {};
  toast = message => notices.push(message);
  startCoreUpdatePolling();
  startCoreUpdatePolling();
  assert.equal(timers.length, 1);
  const first = timers[0]();
  startCoreUpdatePolling();
  assert.equal(responses.length, 1);
  assert.equal(timers.length, 1);
  responses[0]({state: 'downloading', progress: 0.4});
  await first;
  assert.equal(timers.length, 2);
  const second = timers[1]();
  responses[1]({state: 'ready'});
  await second;
  assert.equal(timers.length, 2);
  assert.equal(coreUpdatePollTimer, null);
  assert.equal(notices.length, 1);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")
