"""Execute operator decisions in the real frontend, without matching source text."""
from tests.test_selection_performance import run_selection


def test_reparse_failures_show_reasons_and_retry_only_failed_entries():
    run_selection(r"""
(async () => {
  const dialogs = [], messages = [], requested = [], opened = [];
  document.createElement = () => {
    const node = makeNode();
    node.querySelector = () => ({checked: true});
    node.querySelectorAll = () => {
      node.failureButtons = [...node.innerHTML.matchAll(/data-reparse-failure="(\d+)"/g)]
        .map(match => ({dataset: {reparseFailure: match[1]}}));
      return node.failureButtons;
    };
    return node;
  };
  mkBtn = (text, cls, onclick) => ({textContent: text, onclick});
  modal = options => {
    const dialog = {...options, closed: false, close() {this.closed = true;}};
    dialogs.push(dialog); return dialog;
  };
  taskProgress = () => ({update() {}, close() {}});
  toast = message => messages.push(message);
  openEntryDetail = id => opened.push(id);
  State.entries = [{id: 'good'}, {id: 'bad', invoice_no: 'TEST-002'}];
  globalThis.Api = {
    recognitionPreview: async ids => {
      requested.push(ids); return {invoice: {total: ids.length, pending: ids.length}};
    },
    rerecognizeMaterials: async ([id]) => ({invoice: {
      processed: 1, failed: id === 'bad' ? [{entry_id: id, error: '无法读取 <原发票>'}] : [],
    }}),
  };
  refreshEntries = async () => {State.entries = [];};
  await batchReparse(['good', 'bad']);
  await dialogs[0].footer[1].onclick();
  assert.equal(dialogs[0].closed, true);
  assert.equal(messages.at(-1), '成功 1 张，1 张失败');
  const failures = dialogs[1];
  assert.equal(failures.title, '重新识别失败明细');
  assert.ok(failures.body.innerHTML.includes('发票 TEST-002'));
  assert.ok(failures.body.innerHTML.includes('无法读取 &lt;原发票&gt;'));
  assert.ok(failures.body.innerHTML.includes('role="alert"'));
  assert.equal(failures.body.failureButtons.length, 1);
  failures.body.failureButtons[0].onclick();
  assert.deepEqual(opened, ['bad']);
  assert.equal(failures.closed, false);
  await failures.footer[1].onclick();
  assert.deepEqual(requested, [['good', 'bad'], ['bad']]);
  await dialogs[2].footer[1].onclick();
  assert.equal(messages.at(-1), '1 张失败');
  Api.rerecognizeMaterials = async () => ({invoice: {processed: 1, failed: []}});
  await batchReparse(['good']);
  const successDialog = dialogs.at(-1), dialogCount = dialogs.length;
  await successDialog.footer[1].onclick();
  assert.equal(messages.at(-1), '成功 1 张');
  assert.equal(dialogs.length, dialogCount);
  Api.rerecognizeMaterials = async () => ({invoice: {processed: 0, skipped_current: 1, failed: []}});
  await batchReparse(['good']);
  await dialogs.at(-1).footer[1].onclick();
  assert.equal(messages.at(-1), '跳过 1 张已是最新');
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_list_refresh_updates_batch_counts_without_using_filtered_entry_count():
    run_selection(r"""
(async () => {
  State.batchFilter = 'batch';
  State.quickView = 'complete';
  State.batches = [{id: 'batch', stats: {count: 11, incomplete: 1}}];
  const batch = {id: 'batch', stats: {count: 11, incomplete: 0}, entry_notes: {}};
  let matches = Array.from({length: 11}, (_, i) => ({id: 'e' + i}));
  globalThis.Api = {
    getBatch: async () => batch,
    listBatches: async () => ({batches: [batch], unbatched_count: 3}),
    listEntries: async () => matches,
    invoiceRecognitionNotice: async () => ({}),
  };
  refreshTitleOptions = async () => {};
  currentFilters = () => ({batch_id: State.batchFilter || undefined, status: 'complete'});
  renderEntries = syncFilterControlStates = () => {};
  const displayed = [];
  renderBatchFolders = () => displayed.push(State.batches[0].stats.incomplete);
  await refreshEntries();
  assert.deepEqual(displayed, [0]);
  assert.equal(State.batches[0].stats.count, 11);
  assert.equal(State.entries.length, 11);
  assert.equal(State.unbatchedCount, 3);
  assert.equal(State.currentBatch.stats.incomplete, 0);
  matches = [{id: 'matched'}];
  State.batchFilter = '';
  await refreshEntries();
  assert.equal(State.entries.length, 1);
  assert.equal(State.batches[0].stats.count, 11);
  assert.equal(State.batches[0].stats.incomplete, 0);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_material_and_paid_amount_changes_refresh_batches_with_card_update():
    run_selection(r"""
(async () => {
  State.quickView = 'all';
  State.batches = [{id: 'batch', stats: {count: 11, incomplete: 1}}];
  State.currentBatch = {id: 'batch', stats: {count: 11, incomplete: 1}, entry_notes: {e: '保留备注'}};
  document.querySelector('#filterKeyword').value = '';
  document.querySelector('#filterCheck').value = '';
  let incomplete = 0, calls = 0;
  globalThis.Api = {listBatches: async () => {
    calls++;
    return {batches: [{id: 'batch', stats: {count: 11, incomplete}}]};
  }};
  renderBatchFolders = () => {};
  refreshEntryCard = async () => {
    assert.equal(State.batches[0].stats.incomplete, incomplete);
    assert.equal(State.currentBatch.stats.incomplete, incomplete);
    assert.equal(State.currentBatch.entry_notes.e, '保留备注');
  };
  refreshEntries = async () => {throw new Error('应保留单卡刷新');};
  await syncEntryAfterChange('e', {affectsStatus: true});
  incomplete = 1;
  await syncEntryAfterChange('e', {affectsStatus: true});
  assert.equal(calls, 2);
  await syncEntryAfterChange('e', {notes: true});
  assert.equal(calls, 2);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_batch_stats_failure_keeps_the_entry_list_and_previous_batch_counts():
    run_selection(r"""
(async () => {
  const errors = [];
  State.batches = [{id: 'batch', stats: {count: 11, incomplete: 0}}];
  State.unbatchedCount = 3;
  const previous = State.batches;
  globalThis.Api = {
    listBatches: async () => {throw new Error('读取失败');},
    listEntries: async () => [{id: 'entry'}],
    invoiceRecognitionNotice: async () => ({}),
  };
  refreshTitleOptions = async () => {};
  currentFilters = () => ({});
  renderEntries = syncFilterControlStates = () => {};
  renderBatchFolders = () => {throw new Error('不应清空批次栏');};
  toast = message => errors.push(message);
  await refreshEntries();
  assert.deepEqual(State.entries.map(entry => entry.id), ['entry']);
  assert.equal(State.batches, previous);
  assert.equal(State.unbatchedCount, 3);
  await loadBatches();
  assert.equal(State.batches, previous);
  assert.equal(State.unbatchedCount, 3);
  assert.deepEqual(errors, ['批次统计刷新失败：读取失败', '批次统计刷新失败：读取失败']);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_parser_update_notice_scopes_actions_and_persists_dismissal():
    run_selection(r"""
(async () => {
  const saved = [], opened = [];
  globalThis.Api = {dismissInvoiceRecognitionNotice: async version => {
    saved.push(version); return {version, dismissed: true};
  }};
  batchReparse = async (...args) => opened.push(args);
  State.entries = [{id: 'old', invoice_recognition_outdated: true}, {id: 'current'}];
  State.selected = new Set(['current']);
  State.invoiceRecognitionNotice = {version: 'v1', dismissed: false};
  const node = document.querySelector('#invoiceRecognitionNotice');
  renderInvoiceRecognitionNotice();
  assert.equal(node.classList.contains('hidden'), false);
  assert.ok(document.querySelector('#invoiceRecognitionNoticeText').textContent.includes('1 张'));
  await document.querySelector('#invoiceRecognitionNoticeRun').onclick();
  assert.deepEqual(opened, [[['old'], true]]);
  assert.deepEqual([...State.selected], ['current']);
  await document.querySelector('#invoiceRecognitionNoticeDismiss').onclick();
  assert.deepEqual(saved, ['v1']);
  assert.equal(node.classList.contains('hidden'), true);
  renderInvoiceRecognitionNotice();
  assert.equal(node.classList.contains('hidden'), true);
  State.invoiceRecognitionNotice = {version: 'v2', dismissed: false};
  renderInvoiceRecognitionNotice();
  assert.equal(node.classList.contains('hidden'), false);
  State.entries = [{id: 'current'}];
  renderInvoiceRecognitionNotice();
  assert.equal(node.classList.contains('hidden'), true);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_notice_dismissal_failure_remains_visible_and_retryable():
    run_selection(r"""
(async () => {
  let errors = [];
  globalThis.Api = {dismissInvoiceRecognitionNotice: async () => {throw new Error('写入失败');}};
  toast = message => errors.push(message);
  State.entries = [{id: 'old', invoice_recognition_outdated: true}];
  State.invoiceRecognitionNotice = {version: 'v1', dismissed: false};
  renderInvoiceRecognitionNotice();
  const button = document.querySelector('#invoiceRecognitionNoticeDismiss');
  await button.onclick();
  assert.equal(button.disabled, false);
  assert.equal(document.querySelector('#invoiceRecognitionNotice').classList.contains('hidden'), false);
  assert.deepEqual(errors, ['写入失败']);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_theme_follows_system_and_persists_only_when_requested():
    run_selection(r"""
document.documentElement.style = {};
globalThis.localStorage = {setItem: (key, value) => saved.push([key, value])};
const saved = [];
systemThemeMedia.matches = true;
applyTheme('system');
assert.equal(State.themeMode, 'system');
assert.equal(document.documentElement.dataset.theme, 'dark');
assert.equal(saved.length, 0);
applyTheme('light', {persist: true});
assert.equal(document.documentElement.style.colorScheme, 'light');
assert.deepEqual(saved, [['tidoc.themeMode', 'light']]);
applyTheme('unknown');
assert.equal(State.themeMode, 'system');
""")


def test_filters_keep_working_archived_and_unbatched_scopes_separate():
    run_selection(r"""
for (const id of ['filterTitle', 'filterCheck', 'filterProfile', 'filterKeyword',
  'filterAmountMin', 'filterAmountMax', 'filterDateFrom', 'filterDateTo', 'sortSelect']) {
  document.querySelector('#' + id).value = '';
}
State.quickView = 'all'; State.batchFilter = '';
assert.equal(currentFilters().active_only, true);
State.batchFilter = ARCHIVED_BATCH_ID;
assert.equal(currentFilters().archived_only, true);
assert.equal(currentFilters().active_only, undefined);
State.batchFilter = UNBATCHED_BATCH_ID;
assert.equal(currentFilters().unbatched, true);
State.batchFilter = 'batch-7'; State.paymentCountFilter = 'multiple';
State.quickView = 'warning';
document.querySelector('#filterKeyword').value = '  samples  ';
const f = currentFilters();
assert.equal(f.batch_id, 'batch-7');
assert.equal(f.payment_count, 'multiple');
assert.equal(f.check_status, 'warning');
assert.equal(f.check_open, undefined);
assert.equal(f.keyword, 'samples');
assert.equal(f.active_only, undefined);
""")


def test_payment_amount_mismatch_is_confirmed_and_manual_amount_is_preserved():
    run_selection(r"""
(async () => {
  State.paymentAiEnabled = true;
  const writes = [], prompts = [];
  const entry = {total: '100.00', fields: {paid_amount: {current: '100.00'}}};
  globalThis.Api = {getEntry: async () => entry,
    setRecognizedPaidAmount: async (...args) => writes.push(args)};
  askUseRecognizedPaymentAmount = async (...args) => { prompts.push(args); return 'keep'; };
  await settlePaymentAmountAfterAdd('e', [{paid_amount: '90.00'}]);
  assert.equal(prompts.length, 1);
  assert.equal(writes.length, 0);
  await settlePaymentAmountAfterAdd('e', [{paid_amount: '100.00'}]);
  assert.deepEqual(writes, [['e', '100.00']]);
  entry.fields.paid_amount = {current: '80.00', value_source: 'manual'};
  await settlePaymentAmountAfterAdd('e', [{paid_amount: '90.00'}]);
  assert.equal(prompts.length, 1);
  assert.equal(writes.length, 1);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_loose_materials_match_globally_and_keep_unresolved_files_for_manual_binding():
    run_selection(r"""
(async () => {
  let scope, cleaned;
  const manual = {path: '/stage/manual.png'}, auto = {path: '/stage/auto.png'};
  autoBindMaterialInfos = async (infos, entries) => {scope = entries; return {manual: [manual], auto: [auto]};};
  cleanupDroppedPaths = async paths => {cleaned = paths;};
  refreshEntries = async () => {};
  toast = () => {};
  assert.equal(await handleLooseMaterialInfos([auto, manual], [auto.path, manual.path]), true);
  assert.deepEqual(scope, []);
  assert.deepEqual(cleaned, [auto.path]);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_export_timestamp_uses_local_calendar_and_release_links_handle_prereleases():
    run_selection(r"""
const date = {getFullYear: () => 2026, getMonth: () => 0, getDate: () => 2,
  getHours: () => 3, getMinutes: () => 4};
assert.equal(filenameTimestamp(date), '2026-01-02-03-04');
assert.equal(changelogUrl('v0.1.40'), 'https://tidoc.bitfsae.com/update/changelog.html#v0.1.40');
for (const version of ['0.1.41-beta.1', '']) {
  assert.equal(changelogUrl(version), 'https://tidoc.bitfsae.com/update/changelog.html');
}
""")


def test_export_name_uses_one_common_batch_and_falls_back_for_mixed_selection():
    run_selection(r"""
State.batches = [{id:'a',name:'十月采购'}, {id:'b',name:'十一月采购'}];
State.entries = [{id:'one',batch_id:'a'}, {id:'two',batches:[{id:'a'}]}, {id:'three',batch_id:'b'}, {id:'loose'}];
assert.equal(exportDefaultName(['one','two']), '十月采购');
assert.ok(exportDefaultName(['one','three']).startsWith('报账导出-'));
assert.ok(exportDefaultName(['one','loose']).startsWith('报账导出-'));
assert.ok(exportDefaultName(['missing']).startsWith('报账导出-'));
""")


def test_card_title_tooltip_explains_missing_and_unconfigured_titles():
    run_selection(r"""
State.titleProfiles = [{name: '示例大学'}];
assert.equal(entryTitleTooltip({}), '抬头：未填写');
assert.equal(entryTitleTooltip({title: '示例大学'}), '抬头：示例大学');
assert.equal(entryTitleTooltip({title: '其他单位'}), '抬头：其他单位（方案里没有这个抬头）');
""")


def test_manual_amount_equal_to_invoice_and_refunds_still_require_deliberate_use():
    run_selection(r"""
(async () => {
  State.paymentAiEnabled = true;
  const writes = [], prompts = [];
  const entry = {total:'27.00', fields:{paid_amount:{current:'27.00', value_source:'manual'}}};
  globalThis.Api = {getEntry:async () => entry, setRecognizedPaidAmount:async (...args) => writes.push(args)};
  askUseRecognizedPaymentAmount = async (...args) => {prompts.push(args); return 'keep';};
  await settlePaymentAmountAfterAdd('e', [{paid_amount:'27.00'}]);
  assert.equal(writes.length, 0);
  assert.equal(prompts.length, 0);
  entry.fields.paid_amount.value_source = '';
  await settlePaymentAmountAfterAdd('e', [{paid_amount:'27.00',payment_ai:{needs_review:true,reason:'有退款，核对净支出'}}]);
  assert.equal(writes.length, 0);
  assert.equal(prompts.length, 1);
  assert.equal(prompts[0][4], '有退款，核对净支出');
  assert.equal(moneyText('0.00'), '0.00');
  assert.equal(moneyText(''), '');
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_sort_direction_follows_field_default_and_date_shortcuts_use_local_calendar():
    run_selection(r"""
document.querySelector('#sortSelect').value = 'seller';
State.sortDir = SORT_DEFAULT_DIR.seller;
syncSortDirButton();
assert.equal(currentFilters().sort, 'seller');
assert.equal(currentFilters().sort_dir, 'asc');
assert.equal(document.querySelector('#sortDirBtn').getAttribute('aria-label'), '按名称正序');
document.querySelector('#sortSelect').value = 'amount';
State.sortDir = 'asc';
syncSortDirButton();
assert.equal(currentFilters().sort_dir, 'asc');
assert.equal(document.querySelector('#sortDirBtn').getAttribute('aria-label'), '从低到高');
document.querySelector('#sortSelect').value = '';
assert.equal(currentFilters().sort, 'updated');
const today = new Date(2026, 0, 15);
assert.deepEqual(dateRangeOf('this_month', today), ['2026-01-01', '2026-01-31']);
assert.deepEqual(dateRangeOf('last_month', today), ['2025-12-01', '2025-12-31']);
assert.deepEqual(dateRangeOf('this_year', today), ['2026-01-01', '2026-12-31']);
""")


def test_entry_material_roles_follow_the_scheme_and_skip_hidden_roles():
    run_selection(r"""
const material = (id, label, order, presentation = 'visible') => ({id, label, order, presentation, quick_action: true});
const scheme = {materials: [
  material('invoice', '发票', 0),
  material('inspection_pdf', '查验单', 1),
  material('payment_screenshot', '付款截图', 2, 'hidden'),
  material('physical_image', '实物图', 3),
  material('custom:contract', '合同', 4),
]};
// 只保留新建条目能直接添加的三类材料，隐藏的角色不出现，顺序按方案。
assert.deepEqual(entryMaterialRoles(scheme).map((role) => role.id),
  ['inspection_pdf', 'physical_image']);
// 方案没有声明的内置角色（协议仍会注入）不显示；必需的角色即使没写声明也保留。
const injected = {declared_materials: ['invoice'], materials: [
  material('invoice', '发票', 0),
  material('payment_screenshot', '付款截图', 1),
  material('physical_image', '实物图', 2),
  material('inspection_pdf', '查验单', 3),
]};
assert.deepEqual(entryMaterialRoles(injected).map((role) => role.id), []);
const required = {...injected, materials: injected.materials.map((role) =>
  role.id === 'inspection_pdf' ? {...role, min_count: 1} : role)};
assert.deepEqual(entryMaterialRoles(required).map((role) => role.id), ['inspection_pdf']);
assert.deepEqual(entryMaterialRoles({materials: []}).map((role) => role.id), []);
// 没有方案定义时退回默认的三项（旧条目和未绑定方案仍能建条目）。
assert.deepEqual(entryMaterialRoles(null).map((role) => role.id),
  ['payment_screenshot', 'physical_image', 'inspection_pdf']);
assert.deepEqual(entryMaterialRoles(undefined).map((role) => role.label),
  ['付款截图', '实物图', '查验单 PDF']);
""")
