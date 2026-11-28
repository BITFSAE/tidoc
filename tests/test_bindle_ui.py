"""绑定包预览只列出本次条目所属的报账人。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('payload, expected', [
    ({'profiles': [
        {'id': 'current', 'name': '当前报账人', 'reviewer': '审核甲'},
        {'id': 'other', 'name': '另一报账人', 'reviewer': '审核乙'},
        {'id': 'history-only', 'name': '历史修改人', 'reviewer': '审核丙'},
    ], 'entries': [{'profile_id': 'current'}, {'profile_id': 'other'}, {'profile_id': 'other'}]},
     [('current', '当前报账人', '审核甲'), ('other', '另一报账人', '审核乙')]),
    ({'entries': [{'profile_id': 'legacy', 'profile_name': '旧包报账人', 'reviewer': '旧包审核人'}, {}]},
     [('legacy', '旧包报账人', '旧包审核人'), ('__fallback__', '本机报账人', '本机审核人')]),
    ({'profiles': [{'id': 'history-only', 'name': '历史修改人'}], 'entries': []}, []),
])
def test_profile_choices_follow_actual_entries(payload, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('需要 Node 执行预览身份选择检查')
    source = Path(__file__).resolve().parents[1] / 'tidoc/web/bindle-ui.js'
    script = r"""
const fs = require('fs'), vm = require('vm');
const sandbox = {}; vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + ';this.preview = BindlePreview;', sandbox);
const profiles = sandbox.preview.profiles(JSON.parse(process.argv[2]), {name:'本机报账人', reviewer:'本机审核人'});
console.log(JSON.stringify(profiles.map(p=>[p.sourceId,p.name,p.reviewer])));
"""
    result = subprocess.run([node, '-e', script, str(source), json.dumps(payload)],
                            check=True, capture_output=True, text=True, encoding='utf-8')
    assert json.loads(result.stdout) == [list(profile) for profile in expected]


def test_bindle_ui_conflict_helpers_and_batch_review_reasons():
    node = shutil.which('node')
    if not node:
        pytest.skip('需要 Node 执行前端合并辅助逻辑检查')
    source = Path(__file__).resolve().parents[1] / 'tidoc/web/bindle-ui.js'
    script = r"""
const fs = require('fs'), vm = require('vm');
const sandbox = {}; vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(process.argv[1], 'utf8') + ';this.preview = BindlePreview; this.batch = BindleBatch;',
  sandbox,
);
const entry = {
  import_action: 'conflict',
  merge_preview: {
    has_conflicts: true,
    has_changes: true,
    archived: true,
    items: [
      { key: 'att:payment_screenshot', kind: 'attachment_group', status: 'conflict', default_choice: 'keep_local',
        choices: [{value: 'keep_local', label: '保留本机'}, {value: 'replace', label: '替换'}, {value: 'merge', label: '合并'}] },
      { key: 'field:paid_amount', kind: 'field', status: 'conflict', default_choice: 'keep_local', local_value: '50', incoming_value: '43.20',
        choices: [{value: 'keep_local', label: '保留本机'}, {value: 'use_incoming', label: '采用包内'}] },
    ],
  },
};
const defaults = sandbox.preview.defaultDecisions(entry);
const incomingAtt = sandbox.preview.incomingChoiceFor(entry.merge_preview.items[0]);
const incomingField = sandbox.preview.incomingChoiceFor(entry.merge_preview.items[1]);
const stats = sandbox.preview.summarizeDecisions(entry, {
  'att:payment_screenshot': incomingAtt,
  'field:paid_amount': incomingField,
});
const insp = { verified: true, entries: [entry], profiles: [] };
const reasons = sandbox.batch.reviewReasons(insp);
const summary = sandbox.batch.summarize(insp);
console.log(JSON.stringify({
  isExisting: sandbox.preview.isExisting(entry),
  label: sandbox.preview.actionLabel(entry),
  defaults,
  incomingAtt,
  incomingField,
  stats,
  reasons,
  summary,
}));
"""
    result = subprocess.run([node, '-e', script, str(source)],
                            check=True, capture_output=True, text=True, encoding='utf-8')
    data = json.loads(result.stdout)
    assert data['isExisting'] is True
    assert data['label'] == '已有·待确认'
    assert data['defaults'] == {'att:payment_screenshot': 'keep_local', 'field:paid_amount': 'keep_local'}
    assert data['incomingAtt'] == 'replace'
    assert data['incomingField'] == 'use_incoming'
    assert data['stats'] == {'addCount': 0, 'replaceCount': 1, 'updateCount': 1, 'keepCount': 0, 'remainingConflicts': 0}
    assert any('不同' in r for r in data['reasons'])
    assert any('已归档' in r for r in data['reasons'])
    assert data['summary']['conflictCount'] == 1
    assert data['summary']['allUnchanged'] is False


def test_recognition_summary_distinguishes_newer_and_unknown_results():
    node = shutil.which('node')
    if not node:
        pytest.skip('需要 Node 执行前端预览检查')
    source = Path(__file__).resolve().parents[1] / 'tidoc/web/bindle-ui.js'
    script = r"""
const assert = require('node:assert/strict'), fs = require('fs'), vm = require('vm');
const sandbox = {}; vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + ';this.preview = BindlePreview;', sandbox);
const entries = ['older', 'newer', 'newer', 'unknown', 'changed', 'current', 'missing']
  .map(invoice_recognition_state => ({invoice_recognition_state}));
assert.equal(sandbox.preview.recognitionSummary(entries),
  '1 张可用本机方法重新识别；2 张来自更新的识别方法；1 张的材料或识别设置已变化');
assert.equal(sandbox.preview.recognitionSummary([{invoice_recognition_state: 'unknown'}]), '');
assert.equal(sandbox.preview.recognitionSummary([{invoice_recognition_state: 'current'}]), '');
assert.equal(sandbox.preview.recognitionLabel(entries[3]), '');
assert.equal(sandbox.preview.recognitionLabel(entries[5]), '');
assert.equal(sandbox.preview.recognitionLabel(entries[6]), '');
"""
    result = subprocess.run([node, '-e', script, str(source)],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
