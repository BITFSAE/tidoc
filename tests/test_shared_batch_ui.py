"""Exercise shared-batch controls and the local scheduler in the real frontend."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_selection_performance import run_selection


def run_hub(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行前端源码")
    source = Path(__file__).resolve().parents[1] / "tidoc/web/hub-ui.js"
    harness = r"""
const assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
let dialog, refreshed = 0, nextId = 0;
const timers = new Map(), calls = [], notices = [];
let settings = {connected: true, server_url: 'http://local.test', user_id: 'member', role: 'member'};
let links = {}, tick = {batches: {}, generation: 0};
function el() {
  const children = new Map();
  return {classList: {toggle() {}}, disabled: false, checked: false, value: '', innerHTML: '',
    textContent: '', appendChild() {}, focus() {},
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, el());
      return children.get(selector);
    },
  };
}
const context = vm.createContext({
  assert, console, el, esc: value => String(value), uiIcon: () => '<svg></svg>',
  $: () => null, document: {querySelectorAll: () => []},
  State: {batches: [{id: 'batch', name: '采购'}], batchFilter: 'batch'},
  setTimeout(fn) { const id = ++nextId; timers.set(id, fn); return id; },
  clearTimeout(id) {timers.delete(id);},
  toast: (message) => notices.push(message), confirm: () => true,
  taskProgress: () => ({close() {}}),
  refreshEntries: async () => {refreshed++;}, loadBatches: async () => {},
  mkBtn: (textContent, _style, onclick) => ({...el(), textContent, onclick}),
  modal(options) {dialog = {...options, close() {this.closed = true;}}; return dialog;},
  Api: {
    hubSettings: async () => settings,
    hubBatchLinks: async () => ({batches: links}),
    hubSyncTick: async () => {calls.push('tick'); return tick;},
    hubSyncBatch: async (id) => {calls.push(['sync', id]);},
    hubDetachOtherSpaceEntries: async (id) => {calls.push(['detach', id]); links.batch.other_space_count = 0;},
    hubSetBatchAutoSync: async (id, enabled) => {calls.push(['auto', id, enabled]);},
    hubUnlinkBatch: async (id) => {calls.push(['unlink', id]); links = {};},
    hubListSpaces: async () => ({spaces: [{id: 'nov', name: '十一月', status: 'active'}]}),
    hubLinkBatch: async (...args) => {calls.push(['link', ...args]); return {batch_id: 'batch'};},
  },
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const run = code => vm.runInContext(code, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
"""
    result = subprocess.run(
        [node, "-e", harness + script + "\n})().catch(e => {console.error(e); process.exitCode = 1;});", str(source)],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_only_enabled_batches_schedule_ticks_and_disconnection_cancels_them():
    run_hub(r"""
await run('HubUI.refreshState()');
assert.equal(timers.size, 0);
links = {batch: {space_id: 'nov', auto_sync: false}};
await run('HubUI.refreshState()');
assert.equal(timers.size, 0);
links.batch.auto_sync = true;
await run('HubUI.refreshState()');
assert.equal(timers.size, 1);
await run('HubUI.refreshState()');
assert.equal(timers.size, 1);
settings = {connected: false};
await run('HubUI.refreshState()');
assert.equal(timers.size, 0);
assert.equal(calls.length, 0);
assert.equal(run("HubUI.batchIndicatorHtml({id:'batch',name:'采购'})"), '');
""")


def test_manual_sync_refreshes_only_when_local_data_generation_changes():
    run_hub(r"""
links = {batch: {space_id: 'nov', auto_sync: false, state: 'manual'}};
await run('HubUI.refreshState()');
tick = {batches: links, generation: 2};
await run("HubUI.syncBatch({id:'batch'})");
assert.equal(refreshed, 1);
await run("HubUI.syncBatch({id:'batch'})");
assert.equal(refreshed, 1);
assert.equal(calls.filter(item => Array.isArray(item) && item[0] === 'sync').length, 2);
assert.match(run("HubUI.batchIndicatorHtml({id:'batch',name:'采购'})"), /共享批次/);
links.batch.state = 'conflict';
await run('HubUI.refreshState()');
assert.match(run("HubUI.batchIndicatorHtml({id:'batch',name:'采购'})"), /待核对/);
""")


def test_shared_settings_toggle_and_unlink_preserve_local_data():
    run_hub(r"""
links = {batch: {space_id: 'nov', space_name: '十一月', auto_sync: false, state: 'manual'}};
await run('HubUI.refreshState()');
await run("HubUI.openBatchSharing({id:'batch',name:'采购'})");
assert.equal(dialog.title, '共享设置');
const toggle = dialog.body.querySelector('#hubBatchAutoSync');
toggle.checked = true;
await toggle.onchange();
assert.deepEqual(calls[0], ['auto', 'batch', true]);
assert.match(dialog.body.querySelector('[role="status"]').textContent, /已开启/);
await dialog.footer[0].onclick();
assert.deepEqual(calls[1], ['unlink', 'batch']);
assert.equal(dialog.closed, true);
assert.equal(refreshed, 1);
""")


def test_failed_auto_sync_toggle_restores_the_visible_setting():
    run_hub(r"""
links = {batch: {space_id: 'nov', auto_sync: false, state: 'manual'}};
context.Api.hubSetBatchAutoSync = async () => {throw new Error('保存失败');};
await run('HubUI.refreshState()');
await run("HubUI.openBatchSharing({id:'batch',name:'采购'})");
const toggle = dialog.body.querySelector('#hubBatchAutoSync');
toggle.checked = true;
await toggle.onchange();
assert.equal(toggle.checked, false);
assert.equal(toggle.disabled, false);
assert.deepEqual(notices, ['保存失败']);
assert.equal(timers.size, 0);
""")


def test_old_account_tick_cannot_repaint_the_new_account():
    run_hub(r"""
links = {batch: {space_id:'nov', auto_sync:true, state:'synced'}};
await run('HubUI.refreshState()');
let finish;
context.Api.hubSyncTick = () => new Promise(resolve => {finish = resolve;});
const pending = run("HubUI.syncBatch({id:'batch'})");
await flush();
settings = {connected:false};
await run('HubUI.refreshState()');
finish({batches:{batch:{space_id:'secret',auto_sync:true,state:'synced'}},generation:99});
await pending;
assert.equal(refreshed, 0);
assert.equal(timers.size, 0);
assert.equal(run("HubUI.batchInfo({id:'batch'})"), null);
""")


def test_batch_menu_shares_the_clicked_batch_without_using_filtered_selection():
    run_selection(r"""
let connected = true, shared = null, menu, sharedBatch, syncedBatch;
globalThis.HubUI = {
  isConnected: () => connected, batchInfo: () => shared,
  openBatchSharing: batch => {sharedBatch = batch.id;},
  syncBatch: batch => {syncedBatch = batch.id;}, openBatchChanges() {},
};
document.createElement = () => ({innerHTML:'', children: [],
  appendChild(node) {this.children.push(node);}, setAttribute() {},
});
showContextMenu = node => {menu = node;};
closeEntryMenu = () => {};
State.selected = new Set(['unrelated']);
State.entries = [{id:'filtered-visible'}];
(async () => {
  openBatchMenu(0, 0, {id:'clicked', name:'采购'});
  await menu.children.find(node => node.innerHTML.includes('共享此批次')).onclick();
  assert.equal(sharedBatch, 'clicked');
  assert.deepEqual([...State.selected], ['unrelated']);
  shared = {space_id:'nov'};
  openBatchMenu(0, 0, {id:'clicked', name:'采购'});
  await menu.children.find(node => node.innerHTML.includes('立即同步')).onclick();
  assert.equal(syncedBatch, 'clicked');
  connected = false;
  openBatchMenu(0, 0, {id:'clicked', name:'采购'});
  assert.equal(menu.children.some(node => /共享|同步/.test(node.innerHTML)), false);
})().catch(e => {console.error(e); process.exitCode = 1;});
""")


def test_partial_other_space_routes_to_settings_and_detaches_old_links():
    run_hub(r"""
links = {
  batch: {
    space_id: 'dec', space_name: '十二月', auto_sync: false, state: 'partial',
    conflict_count: 0, other_space_count: 1, other_space_names: ['十一月'],
    message: '1 条仍关联「十一月」，未上传到当前批次',
  },
};
await run('HubUI.refreshState()');
assert.match(run("HubUI.batchIndicatorHtml({id:'batch',name:'采购'})"), /部分未上传/);
await run("HubUI.openBatchChanges({id:'batch',name:'采购'})");
assert.equal(dialog.title, '共享设置');
assert.match(dialog.body.innerHTML, /十一月/);
const detachBtn = dialog.body.querySelector('#hubDetachOtherSpaceBtn');
await detachBtn.onclick();
assert.deepEqual(calls[0], ['detach', 'batch']);
assert.deepEqual(calls[1], ['sync', 'batch']);
assert.equal(dialog.closed, true);
""")
