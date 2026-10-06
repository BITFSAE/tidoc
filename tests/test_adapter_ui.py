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


def test_javascript_parses_in_node_if_available():
    node=shutil.which('node')
    if node:
        for name in ('app.js','api.js','adapter-ui.js','schema-form.js'):
            subprocess.run([node,'--check',str(WEB/name)],check=True,capture_output=True)
