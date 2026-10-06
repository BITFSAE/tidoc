"""Real frontend source integration without browser automation or synthetic pages."""
import re
import shutil
import subprocess
from pathlib import Path

from tidoc.api import Api

ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'tidoc/web'


def test_registered_bridge_methods_exist_and_scripts_are_loaded():
    bridge=(WEB/'api.js').read_text()
    calls=set(re.findall(r"call\('([a-z_]+)'",bridge))
    missing=sorted(method for method in calls if not callable(getattr(Api,method,None)))
    assert not missing
    html=(WEB/'index.html').read_text()
    assert 'schema-form.js' in html and 'adapter-ui.js' in html
    assert html.index('adapter-ui.js')<html.index('app.js')


def test_live_workflow_uses_dynamic_backend_schemes_and_fields():
    source=(WEB/'app.js').read_text()
    adapter=(WEB/'adapter-ui.js').read_text()
    assert '北京理工大学' not in source and '工训楼' not in source
    assert 'AdapterUI.setup()' in source
    assert 'AdapterUI.decorateEntry' in source
    assert 'AdapterUI.cardActions(e)' in source
    assert 'AdapterUI.print(ids)' in source
    assert 'previewExport' in adapter and 'runExport' in adapter
    assert 'role_definition_revision_id' in adapter
    assert 'previewRebind' in adapter and 'applyRebind' in adapter
    assert 'scheme.revision_id' in adapter
    assert 'expected_version' in (ROOT/'tidoc/api.py').read_text()


def test_forms_have_labels_and_focusable_linked_errors():
    source=(WEB/'schema-form.js').read_text()
    assert 'label.htmlFor = id' in source
    assert "setAttribute('aria-describedby'" in source
    assert 'summary.tabIndex = -1' in source
    assert 'control.input.focus()' in source
    assert 'field.type === \'boolean\'' in source
    assert "type === 'integer'" in source


def test_hidden_rows_are_hidden_and_help_text_is_not_a_callout_box():
    css=(WEB/'styles.css').read_text()
    # .form-row 自带 display:flex，会盖过 hidden 属性；条件字段和账户类型联动都靠 hidden。
    assert '.form-row[hidden]' in css
    source=(WEB/'schema-form.js').read_text()
    assert "el('small', 'field-help')" in source
    assert "el('small', 'hint')" not in source


def test_scheme_details_reports_package_baseline_apart_from_local_overrides(api):
    scheme=api.scheme_details()['data']
    baseline=scheme['settings_baseline']
    # 默认输出的包默认值来自 scheme.default_outputs，而不是注册表里的空列表。
    assert baseline['print.default_outputs']==scheme['definition']['scheme']['default_outputs']
    api.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    changed=api.scheme_details(scheme['id'])['data']
    assert changed['definition']['effective_settings']['print.numbering'] is False
    assert changed['settings_baseline']['print.numbering'] is True


def test_scheme_page_saves_only_changed_settings_against_package_baseline():
    adapter=(WEB/'adapter-ui.js').read_text()
    assert 'settings_baseline' in adapter
    assert 'filter((row) => row.isDirty())' in adapter
    assert 'confirmLeave' in adapter


def test_dialog_open_close_rules_live_in_the_shared_modal():
    source=(WEB/'app.js').read_text()
    modal=source.split('function modal(',1)[1].split('function confirmDialog',1)[0]
    # 语义、焦点归还、仅最上层可交互、点遮罩必须"按下和松开都在遮罩上"、关闭前确认、子页面关闭后的回调。
    for needle in ("setAttribute('aria-modal', 'true')","opener.focus","syncModalLayers()","pressedOnMask","guard","_onResume","key &&"):
        assert needle in modal,needle
    # 弹窗打开时列表快捷键（/、n、t、Cmd+A）不作用到后面的页面。
    assert "if ($('#modalRoot').lastChild) return;" in source
    settings=source.split('async function buildSettings',1)[1].split('// 更新对话框里的可选组件',1)[0]
    assert 'confirm(' not in settings
    assert "key: 'settings'" in settings and '_onResume' in settings
    # 设置页的子页面叠在上面，不再先关掉设置页。
    assert 'm.close(); AdapterUI' not in settings
    adapter=(WEB/'adapter-ui.js').read_text()
    assert 'guard: confirmLeave' in adapter
    assert 'mask.onclick' not in adapter and '_closeModal =' not in adapter


def test_javascript_parses_in_node_if_available():
    node=shutil.which('node')
    if node:
        for name in ('app.js','api.js','adapter-ui.js','schema-form.js'):
            subprocess.run([node,'--check',str(WEB/name)],check=True,capture_output=True)
