"""Execute operator decisions in the real frontend, without matching source text."""
from tests.test_selection_performance import run_selection


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
for (const id of ['filterTitle', 'filterStatus', 'filterCheck', 'filterProfile', 'filterKeyword',
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
assert.equal(f.keyword, 'samples');
assert.equal(f.active_only, undefined);
""")


def test_payment_amount_mismatch_is_confirmed_and_manual_amount_is_preserved():
    run_selection(r"""
(async () => {
  State.paymentOcrEnabled = true;
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


def test_card_title_tooltip_explains_missing_and_unconfigured_titles():
    run_selection(r"""
State.titleProfiles = [{name: '示例大学'}];
assert.equal(entryTitleTooltip({}), '抬头：未填写');
assert.equal(entryTitleTooltip({title: '示例大学'}), '抬头：示例大学');
assert.equal(entryTitleTooltip({title: '其他单位'}), '抬头：其他单位（方案里没有这个抬头）');
""")
