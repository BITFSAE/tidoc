"""二维码取票的前端行为：判断是不是取票二维码、取票、失败回落，用 Node 直接执行真实的 app.js / api.js。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_selection_performance import run_selection

WEB = Path(__file__).resolve().parents[1] / "tidoc/web"

# 每个脚本共用的桩：计时器不真跑；一个按文件名配置「预检」和「取票」结果的后端。
PRELUDE = r"""
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
globalThis.clearTimeout = () => {};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const until = async (check) => { for (let i = 0; i < 400 && !check(); i++) await sleep(2); assert.ok(check(), 'condition not reached'); };

function qrError(message, code, extra = {}) { return Object.assign(new Error(message), { code, ...extra }); }

// spec[文件名] = { preview: {...}|{error}, fetch: {...}|{error}|{gate} }
function makeQrApi(spec) {
  const state = { previews: [], fetches: [], inflight: 0, maxInflight: 0 };
  state.api = {
    fetchInvoiceByQr: async (path, options) => {
      const name = baseName(path);
      const rule = spec[name] || {};
      const isPreview = !!(options && options.preview_only);
      (isPreview ? state.previews : state.fetches).push({ name, options });
      state.inflight += 1;
      state.maxInflight = Math.max(state.maxInflight, state.inflight);
      try {
        if (!isPreview && rule.fetch && rule.fetch.gate) await rule.fetch.gate;
        else await sleep(3);
        const step = isPreview ? rule.preview : rule.fetch;
        if (!step) throw qrError('图片里没有找到二维码', 'no_qr');
        if (step.error) throw qrError(step.error, step.code, step);
        if (isPreview) return { preview_only: true, host: 'dppt.example.chinatax.gov.cn', invoice_no: step.invoice_no };
        return step.data;
      } finally { state.inflight -= 1; }
    },
  };
  return state;
}
const ticket = (invoice_no, fetchData) => ({ preview: { invoice_no }, fetch: { data: fetchData } });
const files = (id, kinds = ['xml', 'pdf'], warnings = []) => ({
  invoice_no: id.toUpperCase(), warnings,
  paths: kinds.map((kind) => `/stage/${id}.${kind}`), files: kinds.map((kind) => ({ kind })),
});
"""


def run_web(script: str) -> None:
    run_selection(PRELUDE + script)


def run_api_js(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行前端源码")
    harness = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const sandbox = {assert, console, process, setTimeout, setInterval, clearInterval,
  window: {addEventListener() {}, pywebview: {api: {list_profiles() {}}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
vm.runInContext(process.argv[2], sandbox);
"""
    result = subprocess.run([node, "-e", harness, str(WEB / "api.js"), script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- 后端桥

def test_bridge_forwards_stage_open_url_and_detail_of_qr_failures():
    run_api_js(r"""
window.pywebview.api.fetch_invoice_by_qr = async () => ({
  ok: false, error: '税务平台拒绝了软件的自动下载请求。', code: 'blocked', stage: 'download',
  detail: 'http 403', open_url: 'https://dppt.example.chinatax.gov.cn:8443/v/token',
});
(async () => {
  await assert.rejects(Api.fetchInvoiceByQr('/stage/a.png'), (error) => {
    assert.equal(error.message, '税务平台拒绝了软件的自动下载请求。');
    assert.equal(error.code, 'blocked');
    assert.equal(error.stage, 'download');
    assert.equal(error.detail, 'http 403');
    assert.equal(error.open_url, 'https://dppt.example.chinatax.gov.cn:8443/v/token');
    return true;
  });
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_other_bridge_failures_do_not_gain_qr_fields_and_success_is_unwrapped():
    run_api_js(r"""
window.pywebview.api.list_entries = async () => ({ ok: false, error: '出错了', code: 'x' });
const calls = [];
window.pywebview.api.fetch_invoice_by_qr = async (...args) => { calls.push(args); return { ok: true, data: { paths: ['/s/a.xml'] } }; };
(async () => {
  await assert.rejects(Api.listEntries({}), (error) => {
    assert.equal(error.code, 'x');
    assert.equal('stage' in error, false);
    assert.equal('open_url' in error, false);
    return true;
  });
  assert.deepEqual(await Api.fetchInvoiceByQr('/s/a.png', { preview_only: true }), { paths: ['/s/a.xml'] });
  await Api.fetchInvoiceByQr('/s/b.png');
  assert.deepEqual(calls, [['/s/a.png', { preview_only: true }], ['/s/b.png', null]]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


# ---------------------------------------------------------------- 判断是不是取票二维码

def test_preview_splits_tickets_from_images_that_return_to_the_normal_flow():
    run_web(r"""
const spec = {
  'a.png': ticket('N1'), 'a-copy.png': ticket('N1'), 'b.jpg': ticket('N2'),
  'pay.png': {},                                                       // 没有二维码
  'other.png': { preview: { error: '不是取票链接', code: 'not_invoice_link' } },
  'face.jpg': { preview: { error: '票面二维码', code: 'invoice_face_qr' } },
  'heic.jpg': { preview: { error: '暂不支持 HEIC', code: 'image_unsupported' } },
  'broken.png': { preview: { error: '读取失败' } },                    // 没有错误码也回落
  'changed.png': { preview: { error: '页面可能已改版', code: 'platform_changed', stage: 'link' } },
};
const state = makeQrApi(spec);
(async () => {
  const input = ['a.png', 'pay.png', 'a-copy.png', 'doc.pdf', 'b.jpg', 'other.png', 'face.jpg', 'heic.jpg',
                 'broken.png', 'changed.png', 'anim.gif', 'scan.xml'].map((name) => '/stage/' + name);
  const found = await detectQrTicketImages(input, { api: state.api });
  assert.deepEqual(found.tickets.map((t) => t.name), ['a.png', 'b.jpg']);
  assert.equal(found.tickets[0].invoice_no, 'N1');
  assert.deepEqual(found.duplicates, ['/stage/a-copy.png']);                           // 同一张发票只取一次
  assert.deepEqual(found.rest.map(baseName),
    ['pay.png', 'doc.pdf', 'other.png', 'face.jpg', 'heic.jpg', 'broken.png', 'anim.gif', 'scan.xml']);
  assert.equal(found.faceQr, 1);
  assert.equal(found.unsupported, 1);
  // 软件读不懂的税务平台二维码不能当材料处理，要告诉用户。
  assert.equal(found.failures.length, 1);
  assert.equal(found.failures[0].name, 'changed.png');
  assert.equal(found.failures[0].code, 'platform_changed');
  // 预检只识别、不联网；PDF、XML、GIF 根本不会被送去识别。
  assert.ok(state.previews.every((call) => call.options.preview_only === true));
  assert.equal(state.fetches.length, 0);
  assert.equal(state.previews.some((call) => /\.(pdf|xml|gif)$/.test(call.name)), false);
  assert.ok(state.maxInflight <= 2);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_images_that_are_not_tickets_pass_through_without_noise():
    run_web(r"""
const state = makeQrApi({ 'pay.png': {}, 'shot.jpg': { preview: { error: '不是取票链接', code: 'not_invoice_link' } } });
const notices = [], cleaned = [], updates = [];
(async () => {
  const input = ['/stage/pay.png', '/stage/shot.jpg', '/stage/a.pdf'];
  const result = await expandQrTicketImages(input, { update: (text) => updates.push(text) },
    { api: state.api, notify: (...args) => notices.push(args), cleanup: async (p) => cleaned.push(...p) });
  assert.deepEqual(result.paths, input);
  assert.equal(result.report, null);
  assert.equal(result.cancelled, false);
  assert.deepEqual(notices, []);                      // 不打扰
  assert.deepEqual(cleaned, []);                      // 原有流程还要用这些文件
  assert.equal(state.fetches.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_without_images_nothing_is_called():
    run_web(r"""
const state = makeQrApi({});
(async () => {
  const updates = [];
  const result = await expandQrTicketImages(['/stage/a.pdf', '/stage/b.xml'], { update: (t) => updates.push(t) }, { api: state.api });
  assert.deepEqual(result.paths, ['/stage/a.pdf', '/stage/b.xml']);
  assert.equal(state.previews.length + state.fetches.length, 0);
  assert.deepEqual(updates, []);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_face_qr_and_heic_get_one_gentle_notice_but_still_use_the_normal_flow():
    run_web(r"""
const state = makeQrApi({
  'face.png': { preview: { error: '票面二维码', code: 'invoice_face_qr' } },
  'face2.png': { preview: { error: '票面二维码', code: 'invoice_face_qr' } },
  'heic.jpg': { preview: { error: '暂不支持 HEIC', code: 'image_unsupported' } },
});
const notices = [];
(async () => {
  const input = ['/stage/face.png', '/stage/face2.png', '/stage/heic.jpg'];
  const result = await expandQrTicketImages(input, null, { api: state.api, notify: (...args) => notices.push(args), cleanup: async () => {} });
  assert.deepEqual(result.paths, input);
  assert.equal(notices.length, 1);
  assert.match(notices[0][0], /2 张图片是发票票面上的二维码/);
  assert.match(notices[0][0], /1 张图片是 HEIC/);
  assert.equal(qrFallbackNotice({}), '');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


# ---------------------------------------------------------------- 取票与汇总

def test_mixed_batch_fetches_with_two_lanes_and_reports_each_failure_separately():
    run_web(r"""
const openUrl = 'https://dppt.example.chinatax.gov.cn:8443/v/token';
const state = makeQrApi({
  'a.png': ticket('N1', files('n1')),
  'a-copy.png': ticket('N1', files('n1')),
  'b.png': ticket('N2', files('n2', ['pdf'], ['未能取得发票 XML，只取回了 PDF；识别会以 PDF 文字为准。'])),
  'c.png': { preview: { invoice_no: 'N3' }, fetch: { error: '税务平台拒绝了软件的自动下载请求。', code: 'blocked', stage: 'download', open_url: openUrl } },
  'd.png': { preview: { invoice_no: 'N4' }, fetch: { error: '网络连接失败或超时，请检查网络后重试。', code: 'network', stage: 'download', open_url: openUrl } },
  'e.png': ticket('N5', files('n5')),
  'pay.png': {},
});
const cleaned = [], updates = [], cancelable = [];
(async () => {
  const input = ['a.png', 'pay.png', 'doc.pdf', 'b.png', 'c.png', 'a-copy.png', 'd.png', 'e.png'].map((n) => '/stage/' + n);
  const progress = { update: (t) => updates.push(t), cancelable: (fn) => cancelable.push(typeof fn) };
  const result = await expandQrTicketImages(input, progress, { api: state.api, notify: () => {}, cleanup: async (p) => cleaned.push(...p) });

  // 其余文件原样保留，取回的文件按图片顺序排在后面，可以直接交给原有的识别与导入。
  assert.deepEqual(result.paths, ['/stage/pay.png', '/stage/doc.pdf', '/stage/n1.xml', '/stage/n1.pdf', '/stage/n2.pdf', '/stage/n5.xml', '/stage/n5.pdf']);
  assert.equal(result.cancelled, false);
  assert.equal(result.fetchedFileCount, 5);
  assert.deepEqual(result.report.fetched.map((item) => item.invoice_no), ['N1', 'N2', 'N5']);
  assert.deepEqual(result.report.fetched[1].warnings, ['未能取得发票 XML，只取回了 PDF；识别会以 PDF 文字为准。']);
  assert.equal(result.report.duplicates, 1);
  // 一张失败不影响其余；失败的逐条带原因，有浏览器链接的保留链接。
  assert.deepEqual(result.report.failed.map((item) => [item.name, item.code, item.stage, item.open_url]),
    [['c.png', 'blocked', 'download', openUrl], ['d.png', 'network', 'download', openUrl]]);
  assert.equal(result.report.failed[0].invoice_no, 'N3');
  // 每张发票只联网取一次，且不是预检模式；同时最多两个请求。
  assert.deepEqual(state.fetches.map((call) => call.name).sort(), ['a.png', 'b.png', 'c.png', 'd.png', 'e.png']);
  assert.ok(state.fetches.every((call) => call.options.request_id && !call.options.preview_only));
  assert.ok(state.maxInflight <= 2);
  // 用完的二维码图片（含重复的）删掉，其余文件不动。
  assert.deepEqual(cleaned.map(baseName).sort(), ['a-copy.png', 'a.png', 'b.png', 'c.png', 'd.png', 'e.png']);
  // 取票期间提示条可取消，结束后撤掉。
  assert.deepEqual(cancelable, ['function', 'object']);
  assert.ok(updates.some((text) => text.startsWith('正在从税务平台取票')));
  assert.ok(updates.some((text) => text.includes('已完成 ')));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_cancel_stops_waiting_discards_late_results_and_keeps_other_files():
    run_web(r"""
let releaseA, releaseB;
const gateA = new Promise((resolve) => { releaseA = resolve; });
const gateB = new Promise((resolve) => { releaseB = resolve; });
const state = makeQrApi({
  'a.png': { preview: { invoice_no: 'N1' }, fetch: { gate: gateA.then(() => ({}) ) } },
  'b.png': { preview: { invoice_no: 'N2' }, fetch: { gate: gateB.then(() => ({}) ) } },
  'c.png': ticket('N3', files('n3')),
});
// 门放行后的返回值：让桩在放行后返回带文件的数据
state.api.fetchInvoiceByQr = ((original) => async (path, options) => {
  const result = await original(path, options);
  return options && options.preview_only ? result : files('late-' + baseName(path).slice(0, 1));
})(state.api.fetchInvoiceByQr);

const cleaned = [], notices = [];
let cancel = null;
(async () => {
  const input = ['/stage/a.png', '/stage/b.png', '/stage/c.png', '/stage/pay.png'];
  const progress = { update() {}, cancelable: (fn) => { if (fn) cancel = fn; } };
  const pending = expandQrTicketImages(input, progress, { api: state.api, notify: (...args) => notices.push(args), cleanup: async (p) => cleaned.push(...p) });
  await until(() => state.fetches.length === 2);                       // 两条通道都在等税务平台
  cancel();
  const result = await pending;                                        // 取消后不再等待
  assert.equal(result.cancelled, true);
  assert.deepEqual(result.paths, ['/stage/pay.png']);                  // 其余文件继续走原来的流程
  assert.equal(state.fetches.some((call) => call.name === 'c.png'), false);   // 排队的不再发出
  assert.ok(notices.some(([text]) => text.includes('已取消取票')));
  assert.deepEqual(cleaned.map(baseName).sort(), ['a.png', 'b.png', 'c.png']);
  cleaned.length = 0;
  releaseA(); releaseB();                                              // 请求在后台结束，取回的文件被丢弃
  await until(() => cleaned.length === 4);
  assert.deepEqual(cleaned.sort(), ['/stage/late-a.pdf', '/stage/late-a.xml', '/stage/late-b.pdf', '/stage/late-b.xml']);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_a_ticket_that_returns_no_files_is_reported_instead_of_silently_dropped():
    run_web(r"""
const state = makeQrApi({ 'a.png': ticket('N1', { invoice_no: 'N1', paths: [], files: [], warnings: [] }) });
(async () => {
  const result = await expandQrTicketImages(['/stage/a.png'], null, { api: state.api, notify: () => {}, cleanup: async () => {} });
  assert.deepEqual(result.paths, []);
  assert.equal(result.report.fetched.length, 0);
  assert.equal(result.report.failed.length, 1);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_progress_text_shows_counts_and_long_waits():
    run_web(r"""
assert.equal(qrProgressText(0, 1, 0), '正在从税务平台取票…');
assert.equal(qrProgressText(1, 3, 2), '正在从税务平台取票…（已完成 1/3）');
assert.equal(qrProgressText(0, 1, 12), '正在从税务平台取票…（已等待 12 秒）');
assert.equal(qrProgressText(2, 3, 9), '正在从税务平台取票…（已完成 2/3，已等待 9 秒）');
""")


# ---------------------------------------------------------------- 结果展示

def test_report_lists_failures_with_browser_button_and_escapes_user_text():
    run_web(r"""
const report = {
  fetched: [{ name: 'ok.png', invoice_no: 'N1', warnings: ['只取回了 PDF'] }],
  failed: [
    { name: '<b>x</b>.png', invoice_no: 'N2', error: '税务平台拒绝了软件的自动下载请求。', code: 'blocked', stage: 'download', open_url: 'https://dppt.example.chinatax.gov.cn:8443/v/secret' },
    { name: 'y.png', invoice_no: '', error: '这个取票链接已失效', code: 'link_invalid', stage: 'download', open_url: '' },
    { name: 'z.png', invoice_no: '', error: '页面可能已改版', code: 'platform_changed', stage: 'link', open_url: '' },
  ],
  duplicates: 0,
};
const html = qrReportHtml(report);
assert.ok(!html.includes('<b>x</b>'));                                  // 文件名按文本显示
assert.ok(html.includes('&lt;b&gt;x&lt;/b&gt;.png'));
assert.ok(!html.includes('secret'));                                    // 取票链接不写进页面，点按钮时才取
assert.equal((html.match(/data-qr-open=/g) || []).length, 1);           // 只有带链接的失败才有按钮
assert.ok(html.includes('data-qr-open="0"'));
assert.ok(html.includes('只取回了 PDF'));
assert.ok(html.includes('这个取票链接已失效'));
assert.ok(html.includes('用手机扫描'));                                  // 没读出链接时给出手机扫码的办法

assert.ok(qrReportHtml(report, { standalone: true }).indexOf('二维码取票：') === -1);
assert.equal(qrReportHtml(null), '');
assert.equal(qrReportHtml({ fetched: [], failed: [] }), '');
const quiet = qrReportHtml({ fetched: [{ name: 'a', invoice_no: 'N1', warnings: [] }, { name: 'b', invoice_no: 'N2', warnings: [] }], failed: [] });
assert.ok(quiet.includes('已从税务平台取回 2 张发票的文件'));
assert.ok(!quiet.includes('data-qr-open'));
""")


def test_browser_button_opens_the_validated_link_through_the_existing_bridge():
    run_web(r"""
const opened = [];
globalThis.Api = { openExternalUrl: async (url) => { opened.push(url); } };
let handler = null;
const root = { addEventListener: (type, fn) => { if (type === 'click') handler = fn; } };
bindQrReport(root, { failed: [{ open_url: '' }, { open_url: 'https://dppt.example.chinatax.gov.cn:8443/v/token' }] });
const click = (index) => handler({ target: { closest: () => (index === null ? null : { dataset: { qrOpen: String(index) } }) } });
click(null);
click(0);
click(1);
assert.deepEqual(opened, ['https://dppt.example.chinatax.gov.cn:8443/v/token']);
""")


def test_source_label_names_the_qr_batch_or_the_mix():
    run_web(r"""
const qr = (fetched, fileCount) => ({ report: { fetched: Array.from({ length: fetched }, () => ({})) }, fetchedFileCount: fileCount });
assert.equal(qrSourceLabel('3 个拖入文件', null, 3), '3 个拖入文件');
assert.equal(qrSourceLabel('3 个拖入文件', qr(0, 0), 3), '3 个拖入文件');
assert.equal(qrSourceLabel('4 个拖入文件', qr(2, 4), 4), '2 张取票二维码');
assert.equal(qrSourceLabel('5 个拖入文件', qr(2, 4), 5), '5 个拖入文件（含 2 张取票二维码）');
""")


# ---------------------------------------------------------------- 接入拖入 / 粘贴

DROP_HARNESS = r"""
const handlers = {};
document.addEventListener = (type, fn) => { handlers[type] = fn; };
globalThis.Element = class {};
hideDragOverlay = () => {};
const progressLog = [];
taskProgress = () => ({ update: (t) => progressLog.push(t), close() {}, cancelable() {} });
const toasts = [], previews = [], loose = [], failureDialogs = [], cleaned = [], scans = [];
toast = (message, kind) => toasts.push([message, kind]);
openBatchImportPreview = (scan, label, options) => previews.push({ scan, label, options });
handleLooseMaterialInfos = async (infos, paths) => { loose.push({ infos, paths }); return true; };
openQrFailureDialog = (report) => failureDialogs.push(report);
refreshEntries = async () => {};
const addedToEntry = [];
addMaterialInfosToEntry = async (entryId, infos) => { addedToEntry.push([entryId, infos.length]); return { message: '' }; };
function installApi(state) {
  globalThis.Api = {
    ...state.api,
    classifyMaterialFiles: async (paths) => paths.map((path) => ({ path, name: baseName(path), type: classifyAttachmentByName(path) })),
    scanFiles: async (paths) => { scans.push(paths); return { groups: [], total_files: paths.length }; },
    cleanupDroppedFiles: async (paths) => { cleaned.push(...paths); return { deleted: paths.length }; },
  };
  setupGlobalDrop();
  setupClipboardUpload();
}
const dropEvent = (names) => ({ preventDefault() {}, target: {}, dataTransfer: { files: names.map((n) => ({ name: n, path: '/stage/' + n })) } });
const pasteEvent = (names) => ({ preventDefault() {}, clipboardData: { files: names.map((n) => ({ name: n, path: '/stage/' + n, size: 1, type: 'image/png' })), items: [] } });
"""


def test_dropping_ticket_images_on_the_blank_area_opens_the_usual_import_preview():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'a.png': ticket('N1', files('n1')), 'b.png': ticket('N2', files('n2', ['pdf'], ['只取回了 PDF'])), 'pay.png': {} });
installApi(state);
(async () => {
  await handlers.drop(dropEvent(['a.png', 'b.png', 'pay.png']));
  // 取回的发票文件和拖入发票文件一样交给扫描与「确认导入」窗口；付款截图仍作为随附材料。
  assert.deepEqual(scans, [['/stage/n1.xml', '/stage/n1.pdf', '/stage/n2.pdf']]);
  assert.equal(previews.length, 1);
  assert.equal(previews[0].options.qrReport.fetched.length, 2);
  assert.equal(previews[0].options.pendingMaterialInfos.length, 1);
  assert.equal(previews[0].options.pendingMaterialInfos[0].path, '/stage/pay.png');
  assert.deepEqual(previews[0].options.cleanupPaths, ['/stage/pay.png', '/stage/n1.xml', '/stage/n1.pdf', '/stage/n2.pdf']);
  assert.equal(previews[0].label, '2 张取票二维码');       // 扫描的三个发票文件都来自二维码
  assert.deepEqual(toasts.filter(([, kind]) => kind === 'err'), []);
  assert.equal(failureDialogs.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_ticket_images_dropped_together_with_invoice_files_share_one_import_preview():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'a.png': ticket('N1', files('n1')) });
installApi(state);
(async () => {
  await handlers.drop(dropEvent(['a.png', 'x.pdf']));
  assert.equal(previews.length, 1);
  assert.deepEqual(scans, [['/stage/x.pdf', '/stage/n1.xml', '/stage/n1.pdf']]);
  assert.equal(previews[0].label, '3 个拖入文件（含 1 张取票二维码）');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_only_ticket_images_are_labelled_as_such_and_failures_travel_with_the_preview():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({
  'a.png': ticket('N1', files('n1')),
  'b.png': { preview: { invoice_no: 'N2' }, fetch: { error: '税务平台拒绝了软件的自动下载请求。', code: 'blocked', stage: 'download', open_url: 'https://dppt.example.chinatax.gov.cn:8443/v/t' } },
});
installApi(state);
(async () => {
  await handlers.drop(dropEvent(['a.png', 'b.png']));
  assert.equal(previews.length, 1);
  assert.equal(previews[0].label, '1 张取票二维码');
  assert.equal(previews[0].options.qrReport.failed.length, 1);
  assert.equal(failureDialogs.length, 0);               // 失败已随导入窗口一起展示，不再另弹
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_when_every_ticket_fails_the_failure_dialog_replaces_the_import_preview():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({
  'a.png': { preview: { invoice_no: 'N1' }, fetch: { error: '网络连接失败或超时，请检查网络后重试。', code: 'network', stage: 'download', open_url: 'https://x' } },
});
installApi(state);
(async () => {
  await handlers.drop(dropEvent(['a.png']));
  assert.equal(previews.length, 0);
  assert.equal(failureDialogs.length, 1);
  assert.equal(failureDialogs[0].failed[0].code, 'network');
  assert.deepEqual(toasts.filter(([, kind]) => kind === 'err'), []);
  assert.deepEqual(cleaned, ['/stage/a.png']);          // 用完的二维码图片已清理
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_payment_screenshots_keep_their_original_route_when_no_ticket_qr_is_found():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'pay.png': {} });
installApi(state);
(async () => {
  await handlers.drop(dropEvent(['pay.png']));
  assert.equal(previews.length, 0);
  assert.equal(loose.length, 1);
  assert.deepEqual(loose[0].paths, ['/stage/pay.png']);
  assert.deepEqual(toasts, []);
  assert.deepEqual(cleaned, []);                        // 绑定流程还要用，由它自己清理
  assert.equal(state.fetches.length, 0);                // 从不联网
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_pasted_images_in_an_entry_detail_stay_materials_and_are_never_checked_for_qr():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'shot.png': ticket('N1', files('n1')) });
installApi(state);
State.activeDetailEntryId = 'entry-9';
(async () => {
  await handlers.paste(pasteEvent(['shot.png']));
  assert.equal(state.previews.length + state.fetches.length, 0);
  assert.deepEqual(addedToEntry, [['entry-9', 1]]);
  assert.equal(previews.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_pasted_screenshot_of_a_ticket_qr_outside_the_detail_goes_through_ticket_fetch():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'image.png': ticket('N1', files('n1')) });
installApi(state);
State.activeDetailEntryId = null;
(async () => {
  await handlers.paste(pasteEvent(['image.png']));
  assert.equal(previews.length, 1);
  assert.equal(previews[0].label, '1 张取票二维码');
  assert.deepEqual(scans, [['/stage/n1.xml', '/stage/n1.pdf']]);
  assert.deepEqual(addedToEntry, []);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_dropping_on_a_card_or_in_a_dialog_never_reaches_the_ticket_check():
    run_web(DROP_HARNESS + r"""
const state = makeQrApi({ 'a.png': ticket('N1', files('n1')) });
installApi(state);
class FakeElement extends Element { closest(selector) { return this.region ? this : null; } }
globalThis.Element = FakeElement;
(async () => {
  for (const region of [true]) {
    const target = new FakeElement(); target.region = region;
    await handlers.drop({ ...dropEvent(['a.png']), target });
  }
  assert.equal(state.previews.length + state.fetches.length, 0);
  assert.equal(previews.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


# ---------------------------------------------------------------- 提示与细节

def test_drag_hint_does_not_promise_pdf_or_xml_for_images():
    run_web(r"""
const image = (type = 'image/png') => ({ items: [{ kind: 'file', type }] });
assert.equal(dragItemsAreImages(image()), true);
assert.equal(dragItemsAreImages({ items: [{ kind: 'file', type: 'image/png' }, { kind: 'file', type: 'application/pdf' }] }), false);
assert.equal(dragItemsAreImages({ items: [{ kind: 'file', type: '' }] }), false);      // 类型拿不到时保持原提示
assert.equal(dragItemsAreImages({ items: [] }), false);
assert.equal(dragItemsAreImages(null), false);
assert.equal(dragHintText(false, false), DRAG_HINT_IMPORT);
// 没有方案时不能凭空说出材料名：图片只说取票二维码或材料。
assert.equal(dragHintText(false, true), '松开导入：取票二维码图片，或添加为材料');
assert.ok(!dragHintText(false, true).includes('PDF') && !dragHintText(false, true).includes('XML'));
// 悬停在卡片上永远是绑定材料，不承诺任何具体文件类型。
assert.equal(dragHintText(true, true), '松开添加材料到此条目');
assert.ok(!dragHintText(true, true).includes('PDF') && !dragHintText(true, true).includes('XML'));
""")


def test_pasted_screenshots_without_an_extension_are_saved_as_images():
    run_web(r"""
assert.equal(droppedFileName({ name: '', type: 'image/png' }), 'dropped-file.png');
assert.equal(droppedFileName({ name: 'image', type: 'image/jpeg' }), 'image.jpg');
assert.equal(droppedFileName({ name: 'image.png', type: 'image/png' }), 'image.png');
assert.equal(droppedFileName({ name: 'a.pdf', type: 'application/pdf' }), 'a.pdf');
assert.equal(droppedFileName({ name: '', type: '' }), 'dropped-file');
assert.equal(isQrImagePath('/s/a.PNG') && isQrImagePath('/s/a.jpeg') && isQrImagePath('/s/a.webp') && isQrImagePath('/s/a.bmp'), true);
assert.equal(isQrImagePath('/s/a.gif') || isQrImagePath('/s/a.pdf') || isQrImagePath('/s/png'), false);
""")


def test_the_usage_note_is_dismissed_once_and_stays_dismissed():
    run_web(r"""
const store = new Map(), prefs = [];
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
globalThis.Api = { appPreference: async () => '', setAppPreference: async (k, v) => { prefs.push([k, v]); } };
(async () => {
  await loadQrTipPreference();
  assert.equal(State.qrTipDismissed, false);
  dismissQrTip();
  assert.equal(State.qrTipDismissed, true);
  assert.equal(store.get(QR_TIP_KEY), '1');
  assert.deepEqual(prefs, [[QR_TIP_KEY, '1']]);
  // 重新启动：本地缓存没有、但后端记着已关闭
  State.qrTipDismissed = false; store.clear();
  globalThis.Api.appPreference = async () => '1';
  await loadQrTipPreference();
  assert.equal(State.qrTipDismissed, true);
  assert.equal(store.get(QR_TIP_KEY), '1');
  // 后端出错时当作没关过，不影响启动
  State.qrTipDismissed = true; store.clear();
  globalThis.Api.appPreference = async () => { throw new Error('boom'); };
  await loadQrTipPreference();
  assert.equal(State.qrTipDismissed, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_retry_opens_the_normal_preview_and_a_failed_retry_keeps_the_action_available():
    run_web(r"""
(async () => {
  const button = {disabled: false, setAttribute() {}, removeAttribute() {}};
  const calls = [], previews = [], cleaned = [];
  globalThis.Api = {
    fetchInvoiceByQr: async (path, options) => {calls.push([path, options]); return {paths: ['/stage/retry.xml']};},
    scanFiles: async paths => ({groups: [{files: paths}]}),
  };
  openBatchImportPreview = (...args) => previews.push(args);
  cleanupDroppedPaths = async paths => cleaned.push(...paths);
  toast = () => {};
  const item = {open_url: 'https://dppt.example.chinatax.gov.cn:8443/v/token'};
  await retryQrTicket(item, button);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '已取回');
  assert.equal(calls[0][1].retry_url, item.open_url);
  assert.deepEqual(previews[0][2].cleanupPaths, ['/stage/retry.xml']);
  Api.scanFiles = async () => {throw new Error('scan failed');};
  await retryQrTicket(item, button);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '重试');
  assert.deepEqual(cleaned, ['/stage/retry.xml']);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")
