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
