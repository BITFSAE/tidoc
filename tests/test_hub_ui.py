"""Exercise the real upload dialog with delayed and failed bridge calls."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_upload_stop_and_retry_exclude_successful_entries():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行前端源码")
    source = Path(__file__).resolve().parents[1] / "tidoc/web/hub-ui.js"
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function el() {
  const children = new Map();
  return {
    classList: {toggle() {}},
    disabled: false,
    textContent: '',
    innerHTML: '',
    focus() {},
    querySelector(selector) {
      if (selector === '#hubConflictPullNow') return null;
      if (!children.has(selector)) children.set(selector, el());
      return children.get(selector);
    },
  };
}
let dialog, finishCurrent;
const calls = [];
const context = vm.createContext({
  setTimeout, clearTimeout,
  el, esc: (value) => value, $: () => null,
  State: {entries: [{id: 'b', invoice_no: '0002'}]},
  taskProgress: () => ({close() {}}), toast() {}, refreshEntries() {},
  mkBtn: (textContent, _style, onclick) => ({...el(), textContent, onclick}),
  modal(options) {
    dialog = {...options, mask: el(), close() { this.closed = true; }};
    dialog.body.querySelector('#hubPushSpace').value = 'nov';
    return dialog;
  },
  Api: {
    hubSettings: async () => ({connected: true}),
    hubListSpaces: async () => ({spaces: [{id: 'nov', name: '十一月', status: 'active'}]}),
    hubPreparePush: async () => ({plan_id: 'demo-plan'}),
    hubPushEntries: async (space, ids) => {
      assert.equal(space, 'nov');
      assert.equal(ids.length, 1);
      const id = ids[0];
      calls.push(id);
      if (id === 'b' && calls.filter((key) => key === id).length < 3) {
        return {failed: [{error: '连接中断'}]};
      }
      if (id === 'c') return new Promise((resolve) => { finishCurrent = resolve; });
      return {pushed: [{entry_id: id}]};
    },
  },
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);

(async () => {
  await vm.runInContext("HubUI.refreshState()", context);
  await vm.runInContext("HubUI.openPushDialog(['a', 'b', 'c', 'd'])", context);
  const [cancel, upload] = dialog.footer;
  const status = dialog.body.querySelector('#hubPushStatus');
  const result = dialog.body.querySelector('#hubPushResult');
  const running = upload.onclick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(status.textContent, /第 3 \/ 4 条 · 已上传 1 \/ 4 条/);
  assert.equal(cancel.textContent, '停止上传');
  cancel.onclick();
  assert.match(status.textContent, /随后停止/);
  assert.equal(dialog.guard(), false);
  finishCurrent({pushed: [{entry_id: 'c'}]});
  await running;
  assert.deepEqual(calls, ['a', 'b', 'c']);
  assert.match(result.innerHTML, /已停止。已上传 2 条，失败 1 条，未处理 1 条/);
  assert.match(result.innerHTML, /0002 · 失败/);
  assert.equal(upload.textContent, '继续上传（2 条）');
  dialog.body.querySelector('#hubPushSpace').value = 'other';
  await upload.onclick();
  assert.deepEqual(calls, ['a', 'b', 'c', 'b', 'd']);
  assert.equal(upload.textContent, '重试失败条目（1 条）');
  await upload.onclick();
  assert.deepEqual(calls, ['a', 'b', 'c', 'b', 'd', 'b']);
  assert.equal(dialog.closed, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
