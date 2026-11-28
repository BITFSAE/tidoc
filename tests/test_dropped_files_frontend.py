"""拖入文件的前端取路径逻辑，用 Node 直接执行真实的 app.js。"""

from tests.test_selection_performance import run_selection


def test_bindle_entry_points_preview_the_scheme_used_for_import():
    run_selection(r"""
State.currentProfileId = 'person';
State.scheme = {id: 'selected-scheme'};
const calls = [];
globalThis.Api = {inspectBindle: async (path, scheme) => {
  calls.push([path, scheme]); return {entries: []};
}};
taskProgress = () => ({close() {}});
openBindleImportPreview = async () => {};
(async () => {
  await importBindleFlow('/chosen.tidoc');
  await openInboundBindle([{type: 'bindle_package', path: '/dropped.tidoc'}], []);
  assert.deepEqual(calls, [
    ['/chosen.tidoc', 'selected-scheme'], ['/dropped.tidoc', 'selected-scheme']
  ]);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")


def test_dropped_files_use_native_paths_without_reading_their_content():
    run_selection(r"""
const calls = [];
globalThis.FileReader = class { readAsDataURL() { calls.push('read'); } };
globalThis.Api = {
  takeDroppedPaths: async (files) => { calls.push(['take', files]); return { paths: ['/stage/a.pdf', '/stage/b.png'] }; },
  saveDroppedFiles: async () => { calls.push('save'); return { paths: [] }; },
};
(async () => {
  const files = [{ name: 'a.pdf', size: 10 }, { name: 'b.png', size: 20 }];
  const paths = await droppedFilesToPaths(files, { dropped: true });
  assert.deepEqual(paths, ['/stage/a.pdf', '/stage/b.png']);
  assert.deepEqual(calls, [['take', [{ name: 'a.pdf', size: 10 }, { name: 'b.png', size: 20 }]]]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_dropped_files_fall_back_to_reading_content_when_the_native_layer_has_no_path():
    run_selection(r"""
const calls = [];
globalThis.FileReader = class {
  readAsDataURL(file) { calls.push('read ' + file.name); this.result = 'data:;base64,AAAA'; this.onload(); }
};
globalThis.Api = {
  takeDroppedPaths: async () => ({ paths: null }),
  saveDroppedFiles: async (payload) => { calls.push(['save', payload.map((item) => item.name)]); return { paths: ['/stage/a.pdf'] }; },
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'a.pdf', size: 10 }], { dropped: true });
  assert.deepEqual(paths, ['/stage/a.pdf']);
  assert.deepEqual(calls, ['read a.pdf', ['save', ['a.pdf']]]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_pasted_files_never_wait_for_a_native_path():
    run_selection(r"""
let asked = false;
globalThis.FileReader = class {
  readAsDataURL() { this.result = 'data:;base64,AAAA'; this.onload(); }
};
globalThis.Api = {
  takeDroppedPaths: async () => { asked = true; return { paths: null }; },
  saveDroppedFiles: async () => ({ paths: ['/stage/clip.png'] }),
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'clip.png', size: 5 }]);
  assert.deepEqual(paths, ['/stage/clip.png']);
  assert.equal(asked, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_files_that_already_carry_a_path_skip_both_routes():
    run_selection(r"""
globalThis.Api = {
  takeDroppedPaths: async () => { throw new Error('should not ask'); },
  saveDroppedFiles: async () => { throw new Error('should not save'); },
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'a.pdf', path: '/x/a.pdf' }], { dropped: true });
  assert.deepEqual(paths, ['/x/a.pdf']);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_bindle_batch_analysis_handles_review_reasons_duplicates_and_overlaps():
    run_selection(r"""
assert.deepEqual(BindleBatch.reviewReasons({verified: false, entries: [{}]}), ['完整性校验未通过']);
assert.deepEqual(BindleBatch.reviewReasons({verified: true, entries: []}), ['包内没有条目']);
assert.deepEqual(
  BindleBatch.reviewReasons({
    verified: true,
    entries: [{
      role_mapping_preview: {requires_explicit_mapping: ['custom:contract']},
      adapter_compatibility: {missing_capabilities: ['rule.x']},
      extension_merge_preview: {skipped: 1},
    }],
    profile_mapping_preview: {p1: {status: 'ambiguous'}},
  }),
  [
    '存在同名报账人，需确认对应身份',
    '含需手动对应的自定义材料',
    '来源方案有本机未支持的规则',
    '部分附加信息无法自动合并',
  ],
);

const names = BindleBatch.disambiguatePaths([
  '/Users/a/电控组/九月材料.tidoc',
  '/Users/a/机械组/九月材料.tidoc',
  '/Users/a/data/dropped/abcd1234/九月材料.tidoc',
  '/Users/a/采购组.tidoc',
]);
assert.deepEqual(names, [
  {name: '九月材料.tidoc', dir: '电控组'},
  {name: '九月材料.tidoc', dir: '机械组'},
  {name: '九月材料.tidoc', dir: ''},
  {name: '采购组.tidoc', dir: ''},
]);

const rows = [
  {
    path: 'a.tidoc', displayName: 'a.tidoc', state: 'ready', checked: true, userToggled: false,
    insp: {archive_digest: 'd1', entries: [{invoice_no: 'INV-1', total: '100'}, {invoice_no: 'INV-2', total: '50'}]},
    summary: {allUnchanged: false},
  },
  {
    path: 'a-copy.tidoc', displayName: 'a-copy.tidoc', state: 'ready', checked: true, userToggled: false,
    insp: {archive_digest: 'd1', entries: [{invoice_no: 'INV-1', total: '100'}, {invoice_no: 'INV-2', total: '50'}]},
    summary: {allUnchanged: false},
  },
  {
    path: 'b.tidoc', displayName: 'b.tidoc', state: 'ready', checked: true, userToggled: false,
    insp: {archive_digest: 'd2', entries: [{invoice_no: 'INV-2', total: '50'}, {invoice_no: 'INV-3', total: '30'}]},
    summary: {allUnchanged: false},
  },
];
BindleBatch.analyzeRows(rows);
assert.equal(rows[1].state, 'duplicate');
assert.equal(rows[1].duplicateOf, 'a.tidoc');
assert.equal(rows[1].checked, false);
assert.equal(rows[2].overlapCount, 1);

// Removing the first copy promotes the duplicate back to ready.
rows.shift();
BindleBatch.analyzeRows(rows);
assert.equal(rows[0].state, 'ready');
assert.equal(rows[0].duplicateOf, '');
assert.equal(rows[0].checked, true);
assert.equal(rows[1].overlapCount, 1);
""")


def test_batch_bindles_preinspect_share_new_batch_handle_failures_stop_and_append():
    run_selection(r"""
const byId = new Map();
function makeEl() {
  const node = makeNode();
  return Object.assign(node, {
    value: '', checked: false, hidden: false, innerHTML: '', children: [],
    addEventListener() {}, append(...items) { this.children.push(...items); },
    remove() {}, focus() {},
    querySelector(sel) {
      if (sel.startsWith('#')) {
        const key = sel.slice(1);
        if (!byId.has(key)) byId.set(key, makeEl());
        return byId.get(key);
      }
      if (sel === '[name="bindleBatchDestMode"]:checked') return radios.find(r => r.checked) || null;
      if (sel === '[name="bindleBatchDestMode"][value="none"]') return radios[0];
      if (sel === 'input' && this === byId.get('bindleBatchExistingSeg')) return radios[1];
      return null;
    },
    querySelectorAll: sel => sel === '[name="bindleBatchDestMode"]' ? radios : [],
  });
}
const radios = ['none', 'existing', 'new'].map(v => Object.assign(makeEl(), {value: v, checked: v === 'none'}));
document.createElement = makeEl;
globalThis.AdapterUI = {chooser: () => ''};
globalThis.enhanceNativeSelects = globalThis.toast = () => {};
let dialogOpts;
const maskNode = makeEl();
modal = options => { dialogOpts = options; return {mask: maskNode, close() { options.onClose?.(); }}; };
document.querySelector = sel => sel === '#modalRoot' ? {lastChild: maskNode} : makeEl();
State.batches = [];
State.allTags = ['历史标签'];
State.currentProfileId = 'person';
State.scheme = {id: 'scheme-1'};

const inspectCalls = [];
const importCalls = [];
let failOnce = true;
globalThis.Api = {
  listBatches: async () => ({batches: [{id: 'b-old', name: '旧批次', archived: false, stats: {count: 1}}]}),
  listTags: async () => ['电控'],
  inspectBindle: async (path, scheme) => {
    inspectCalls.push([path, scheme]);
    if (path === 'invalid') throw Error('无法读取');
    if (path === 'tampered') return {verified: false, archive_digest: 'dt', entries: [{invoice_no: 'T1', total: '10'}]};
    if (path === 'unchanged') return {verified: true, archive_digest: 'du', entries: [{invoice_no: 'U1', total: '20', import_action: 'unchanged'}]};
    if (path === 'dup') return {verified: true, archive_digest: 'd-a', entries: [{invoice_no: 'I-a', total: '50'}]};
    return {verified: true, archive_digest: 'd-' + path, entries: [{invoice_no: 'I-' + path, total: '50'}]};
  },
  importBindle: async (path, person, allow, options) => {
    assert.equal(allow, false);
    importCalls.push([path, {...options}]);
    if (path === 'flaky' && failOnce) { failOnce = false; throw Error('临时写入失败'); }
    return {
      imported: 1, updated: 0, skipped: [],
      batch_id: options.batch_name ? 'created-batch-1' : (options.batch_id || ''),
      message: '新增 1 条。',
    };
  },
};
loadProfiles = loadBatches = refreshEntries = refreshTagOptions = async () => {};
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

(async () => {
  await openInboundBindle(['a', 'invalid', 'tampered', 'unchanged', 'dup', 'flaky', 'b'].map(path => ({path, type: 'bindle_package'})), []);
  // Wait for pre-inspection of all 7 packages.
  await flush();
  assert.deepEqual(inspectCalls.map(c => c[0]), ['a', 'invalid', 'tampered', 'unchanged', 'dup', 'flaky', 'b']);

  // Choose 新建批次 + 统一标签 + 已有条目也移入此批次.
  radios[0].checked = false;
  radios[2].checked = true;
  byId.get('bindleBatchNewName').value = '十一月汇总';
  byId.get('bindleBatchTag').value = '电控';
  byId.get('bindleBatchMoveExisting').checked = true;

  // Run batch import: should only import ready packages (a, flaky, b); dup & unchanged & tampered & invalid skipped.
  // First package 'a' creates 'created-batch-1'; 'flaky' fails once; 'b' MUST reuse 'created-batch-1' (no duplicate batch!).
  await dialogOpts.footer[2].onclick();
  assert.deepEqual(importCalls, [
    ['a', {scheme_id: 'scheme-1', tags: ['电控'], move_existing_entries: true, batch_name: '十一月汇总'}],
    ['flaky', {scheme_id: 'scheme-1', tags: ['电控'], move_existing_entries: true, batch_id: 'created-batch-1'}],
    ['b', {scheme_id: 'scheme-1', tags: ['电控'], move_existing_entries: true, batch_id: 'created-batch-1'}],
  ]);

  // Retry: only 'flaky' remains checked and reuses the same created batch id!
  await dialogOpts.footer[2].onclick();
  assert.equal(importCalls.length, 4);
  assert.deepEqual(importCalls[3], ['flaky', {scheme_id: 'scheme-1', tags: ['电控'], move_existing_entries: true, batch_id: 'created-batch-1'}]);

  // Appending more files while an earlier inspection is still in flight must not leave earlier rows stuck in checking.
  let releaseCInspect;
  const origInspect = Api.inspectBindle;
  Api.inspectBindle = async (path, scheme) => {
    if (path === 'c') {
      await new Promise(resolve => { releaseCInspect = resolve; });
    }
    return origInspect(path, scheme);
  };
  const added = await openInboundBindle([{path: 'c', type: 'bindle_package'}, {path: 'd', type: 'bindle_package'}], []);
  assert.equal(added, true);
  await Promise.resolve();
  // Add 'e' before 'c' finishes inspecting; 'c', 'd', and 'e' must all finish inspecting.
  await openInboundBindle([{path: 'e', type: 'bindle_package'}], []);
  releaseCInspect();
  Api.inspectBindle = origInspect;
  await flush();
  assert.ok(inspectCalls.some(c => c[0] === 'c'));
  assert.ok(inspectCalls.some(c => c[0] === 'd'));
  assert.ok(inspectCalls.some(c => c[0] === 'e'));
  assert.equal(dialogOpts.footer[2].disabled, false);

  // Mid-batch stop: clicking 停止 while 'c' is importing finishes 'c' and stops before 'd' and 'e'.
  let releaseC;
  const origImport = Api.importBindle;
  Api.importBindle = async (path, person, allow, options) => {
    if (path === 'c') {
      await new Promise(resolve => { releaseC = resolve; });
    }
    return origImport(path, person, allow, options);
  };
  const stopRun = dialogOpts.footer[2].onclick();
  await flush();
  dialogOpts.footer[1].onclick(); // click 「停止」
  releaseC();
  await stopRun;
  assert.equal(importCalls.length, 5);
  assert.equal(importCalls[4][0], 'c');
  assert.equal(importCalls.some(c => c[0] === 'd'), false);

  // Clipboard dedup keeps two identical files when present in files list.
  const file = {name:'same.tidoc',size:32,type:''};
  const item = {kind:'file',getAsFile:() => file};
  assert.equal(filesFromClipboard({clipboardData:{files:[file,file],items:[item,item]}}).length, 2);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")


def test_secondary_launch_opens_batch_dialog_for_multiple_files():
    run_selection(r"""
State.currentProfileId = 'person';
window.ActiveBindleBatch = null;
document.querySelector = () => ({lastChild: null});
const queued = ['/tmp/1.tidoc', '/tmp/2.tidoc'];
globalThis.Api = {
  takeLaunchFile: async () => ({path: queued.shift() || ''}),
};
let batchPaths = null;
openBindleBatch = paths => { batchPaths = paths; };
(async () => {
  await handleSecondaryLaunch();
  assert.deepEqual(batchPaths, ['/tmp/1.tidoc', '/tmp/2.tidoc']);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")
