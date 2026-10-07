"""Selection behavior and work bounds, using the real frontend in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "tidoc/web/app.js"


def run_selection(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行前端源码")
    harness = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
let selectionReads = 0;
function makeNode(id = '') {
  const classes = new Set(), attrs = new Map();
  const node = {dataset: {entryId: id}, textContent: '', disabled: false, tabIndex: -1,
    classList: {
      contains(name) { if (name === 'selected') selectionReads++; return classes.has(name); },
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      remove(name) { classes.delete(name); },
    },
    setAttribute(name, value) { attrs.set(name, value); },
    getAttribute(name) { return attrs.get(name) ?? null; },
    querySelector() { return this.label ??= makeNode(); },
  };
  return node;
}
const controls = new Map();
const document = {documentElement: {dataset: {}}, querySelector(selector) {
  if (!controls.has(selector)) controls.set(selector, makeNode());
  return controls.get(selector);
}};
const sandbox = {document, assert, makeNode,
  resetReads: () => { selectionReads = 0; }, readCount: () => selectionReads,
  window: {matchMedia: () => ({}), addEventListener() {}},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
vm.runInContext(`
  State.entries = Array.from({length: 2000}, (_, index) => ({id: 'entry-' + index}));
  State.entries.forEach((entry, index) => {
    entryCardNodes.set(entry.id, makeNode(entry.id));
    entryDataPositions.set(entry.id, index);
  });
`, sandbox);
vm.runInContext(process.argv[2], sandbox);
"""
    subprocess.run([node, "-e", harness, str(SOURCE), script], check=True,
                   capture_output=True, text=True, encoding="utf-8")


def test_clear_selection_scales_with_selected_cards_and_drops_hidden_selections():
    run_selection(r"""
selectEntryFromCard('entry-7', false);
selectEntryFromCard('entry-1500', false);
State.selected.add('filtered-out');
resetReads();
clearEntrySelection();
assert.equal(State.selected.size, 0);
assert.equal(State.lastSelectedId, null);
assert.equal(readCount(), 2);  // The other 1998 cards are untouched.
for (const id of ['entry-7', 'entry-1500']) {
  assert.equal(entryCardNodes.get(id).getAttribute('aria-selected'), 'false');
}
assert.equal(document.querySelector('#selCount').textContent, '选择条目');
""")


def test_repeated_select_all_does_not_revisit_cards_and_updates_the_action():
    run_selection(r"""
State.selected.add('filtered-out');
selectAllVisible();
assert.equal(State.selected.size, 2000);
assert.equal(State.selected.has('filtered-out'), false);
assert.equal(document.querySelector('#selectAllBtn').label.textContent, '取消全选');
resetReads();
selectAllVisible();
assert.equal(readCount(), 0);
toggleSelectAllVisible();
assert.equal(State.selected.size, 0);
assert.equal(document.querySelector('#selectAllBtn').label.textContent, '全选');
""")


def test_range_selection_keeps_group_counts_current_and_handles_a_stale_anchor():
    run_selection(r"""
const group = {button: makeNode(), size: 3, selectedCount: 0};
for (let index = 0; index < 3; index++) entrySelectionGroups.set('entry-' + index, group);
selectEntryFromCard('entry-0', false);
selectEntryFromCard('entry-2', true);
assert.equal(State.selected.size, 3);
assert.equal(group.selectedCount, 3);
assert.equal(group.button.textContent, '✓');
selectEntryFromCard('entry-1', false);
assert.equal(group.selectedCount, 2);
assert.equal(group.button.textContent, '');
State.lastSelectedId = 'filtered-out';
selectEntryFromCard('entry-1', true);
assert.equal(group.selectedCount, 3);
clearEntrySelection();
assert.equal(group.selectedCount, 0);
assert.equal(group.button.textContent, '');
""")
