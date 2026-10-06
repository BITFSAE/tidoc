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


def test_card_and_detail_keep_the_original_material_workflow():
    source=(WEB/'app.js').read_text()
    adapter=(WEB/'adapter-ui.js').read_text()
    card=source.split('function entryCard(',1)[1].split('right.querySelectorAll',1)[0]
    # 卡片仍是 发票/实付/付款/查验(/实物) 一行，带完成状态、右键打开和在线查验；方案只补自定义材料按钮。
    for needle in ("actionBtn('invoice'","actionBtn('paid'","actionBtn('pay'","actionBtn('inspect'","actionBtn('physical'",'AdapterUI.cardActions(e)'):
        assert needle in card,needle
    assert 'onlineVerificationFlow(e.id)' in source
    cards=adapter.split('function cardActions(',1)[1].split('function jobs(',1)[0]
    assert 'action-state' in cards and 'oncontextmenu' in cards
    assert 'data-card-materials' not in adapter
    # 详情页的材料分组由 app.js 渲染；方案信息只是附加的折叠区，不能隐藏或搬走原来的材料区与拖放区。
    decorate=adapter.split('function decorateEntry(',1)[1].split('function chooser(',1)[0]
    assert 'hidden=true' not in decorate and 'materialDrop' not in decorate
    # 方案信息区放在详情页最末，且不自动展开。
    assert 'body.append(section)' in decorate and 'section.open' not in decorate and 'before(' not in decorate
    assert 'data-add-att-role' in source and 'data-online-verification' in source


def test_list_entries_expose_role_counts_for_custom_material_cards(api):
    profile=api.profiles.create('Role','')
    entry_id=api.entries.create(profile['id'])
    api.db.conn.execute("INSERT INTO attachments(id,entry_id,type,original_name,stored_path,sha256,role_id,added_at) "
                        "VALUES('a1',?,'invoice_pdf','a.pdf','a.pdf','h1','invoice','now')",(entry_id,))
    api.db.conn.commit()
    listed={e['id']:e for e in api.entries.list()}[entry_id]
    assert listed['role_counts']=={'invoice':1}
    assert '_role_counts' not in listed


def test_tooltips_stay_above_dropdown_menus_and_option_tips_only_show_when_truncated():
    css=(WEB/'styles.css').read_text()
    z=lambda selector:int(re.search(re.escape(selector)+r'\s*\{[^}]*?z-index:\s*(\d+)',css).group(1))
    assert z('.fast-tooltip')>z('.select-menu')>z('.entry-context-menu')
    select=(WEB/'select.js').read_text()
    assert 'MENU_Z = 400' in select and z('.select-menu')==400
    menu=select.split('function renderMenu(',1)[1].split('function openMenu(',1)[0]
    assert 'title=' not in menu and 'data-tooltip-overflow' in menu
    app=(WEB/'app.js').read_text()
    tooltips=app.split('function setupFastTooltips()',1)[1].split('let autoUpdateCheckTimer',1)[0]
    # 展开下拉时只显示列表内的提示；提示放在列表侧边；点击或按键后立即收起。
    for needle in ("'.select-menu:not([hidden])'","target.closest('.select-menu')","addEventListener('pointerdown', hide, true)","addEventListener('keydown', hide, true)"):
        assert needle in tooltips,needle


def test_titles_without_a_color_get_distinct_stripe_colors():
    css=(WEB/'styles.css').read_text()
    adapter=(WEB/'adapter-ui.js').read_text()
    app=(WEB/'app.js').read_text()
    assert 'titleColors(State.titleProfiles)' in adapter
    # 色条与分组标题按语义色上色（卡片类名已带 title- 前缀，不再重复拼接）。
    assert ".entry-card:is(.title-blue" in css and ".group-head:is(.title-blue" in css
    for color in ('blue','green','amber','purple','teal','red'):
        assert f'.title-{color} {{ --tc:' in css
    assert "' title-' + tcls" not in app and "(tcls ? ' ' + tcls : '')" in app


def test_card_has_no_checkbox_and_stripe_names_the_title():
    source=(WEB/'app.js').read_text()
    css=(WEB/'styles.css').read_text()
    card=source.split('function entryCard(',1)[1].split('function entryCards(',1)[0]
    # 选中靠点击卡片；复选框与"切换选中"的色条按钮都是重复入口，色条只负责标明抬头。
    assert "entry-check" not in source and 'entry-check' not in css
    assert '切换选中' not in source and 'entryTitleTooltip(e)' in card
    assert "card.append(stripe, main, right)" in card
    assert 'function entryTitleTooltip(' in source and '抬头：' in source
    # 卡片上不重复显示已知信息：单一报账人／已按报账人筛选、正在查看的批次。
    assert "State.profiles.length > 1 && !$('#filterProfile')?.value" in card
    assert 'batch.id !== focusedBatchId' in card


def test_entries_expose_their_own_verification_setting(api):
    profile=api.profiles.create('Verify','')
    entry_id=api.entries.create(profile['id'])
    assert api.entries.get(entry_id)['verification_visible'] is True
    assert {e['id']:e for e in api.entries.list()}[entry_id]['verification_visible'] is True


def test_titles_and_material_requirements_live_in_the_scheme_page():
    app=(WEB/'app.js').read_text()
    adapter=(WEB/'adapter-ui.js').read_text()
    api=(WEB/'api.js').read_text()
    settings=app.split('async function buildSettings',1)[1].split('// 更新对话框里的可选组件',1)[0]
    # 设置页不再有「扩展」：抬头与材料要求属于方案，统一在方案页编辑并随「保存更改」一起提交。
    assert '扩展' not in settings and 'data-material-requirement' not in settings and 'setTitleProfiles' not in app
    assert "TITLES_KEY" in adapter and "REQUIREMENT_PREFIX" in adapter
    assert 'Api.updateScheme(scheme.id, scheme.revision_id' in adapter
    assert "updateScheme: (id,revision,changes) => call('update_scheme',id,revision,changes||{})" in api
    assert 'setMaterialRequirements' not in api and 'setTitleProfiles' not in api
    # 与普通设置共用未保存提示、恢复默认和一次性保存。
    assert 'titles_baseline' in adapter and 'requirements_baseline' in adapter
    assert "if (changes.titles) { await refreshTitleOptions(); renderEntries(); }" in adapter
