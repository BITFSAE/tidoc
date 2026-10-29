"""新增查验单必须确认替换或保留；取消和失败保留原材料。"""

import tempfile
import time
from pathlib import Path

import pytest

from tests.test_inspection_pdf import make_pdf, new_entry, NUMBER


def existing_entry(api, tmp_path):
    entry_id = new_entry(api)
    old = api.add_attachment(entry_id, str(make_pdf(tmp_path / 'old.pdf', 1)), 'inspection_pdf')['data']
    return entry_id, old


def decision(attachments, action='replace', target=None):
    return {'inspection_action': action, 'inspection_existing_ids': [a['id'] for a in attachments],
            'inspection_replace_id': target or attachments[-1]['id']}


def test_manual_add_requires_confirmation_and_replaces_only_selected_file(api, tmp_path):
    entry_id, old = existing_entry(api, tmp_path)
    old_path = Path(old['abs_path'])
    before = old_path.read_bytes()
    new = make_pdf(tmp_path / 'new.pdf', 1, '2026-10-30 10:00:00')
    pending = api.add_attachment(entry_id, str(new), 'inspection_pdf')
    assert pending['ok'] is False
    assert pending['code'] == 'inspection_conflict'
    assert pending['detail']['existing_ids'] == [old['id']]
    assert old_path.read_bytes() == before
    assert len(api.attachments.list(entry_id)) == 1
    result = api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old]))
    assert result['ok'], result
    assert result['data']['id'] == old['id']
    assert Path(result['data']['abs_path']).read_bytes() == new.read_bytes()
    assert len(api.attachments.list(entry_id)) == 1
    assert api.entries.get(entry_id)['check_status'] == 'pass'


def test_keeping_multiple_pdfs_warns_even_when_both_are_first_checks(api, tmp_path):
    entry_id, old = existing_entry(api, tmp_path)
    complete = api.entries.get(entry_id)['completeness']
    other = make_pdf(tmp_path / 'other.pdf', 1, '2026-10-30 10:00:00')
    result = api.add_attachment(entry_id, str(other), 'inspection_pdf', options=decision([old], 'keep'))
    assert result['ok'], result
    entry = api.entries.get(entry_id)
    assert '2 份查验单' in entry['check_message']
    assert entry['check_status'] == 'warning'
    assert entry['completeness'] == complete
    api.entries.set_check(entry_id, 'pass')
    assert '2 份查验单' in api.entries.get(entry_id)['check_message']
    api.delete_attachment(result['data']['id'])
    assert api.entries.get(entry_id)['check_status'] == 'pass'


def test_stale_confirmation_and_foreign_replacement_target_never_overwrite(api, tmp_path):
    entry_id, old = existing_entry(api, tmp_path)
    another = api.attachments.add(entry_id, make_pdf(tmp_path / 'second.pdf', 1, '2026-10-30 10:00:00'), 'inspection_pdf')
    new = make_pdf(tmp_path / 'third.pdf', 1, '2026-10-31 10:00:00')
    stale = api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old]))
    assert stale['code'] == 'inspection_conflict'
    assert set(stale['detail']['existing_ids']) == {old['id'], another['id']}
    wrong = api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old, another], target='foreign'))
    assert wrong['code'] == 'inspection_conflict'
    assert {a['id'] for a in api.attachments.list(entry_id)} == {old['id'], another['id']}
    replaced = api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old, another], target=old['id']))
    assert replaced['ok'], replaced
    assert Path(api.attachments.get(another['id'])['abs_path']).is_file()
    assert '2 份查验单' in api.entries.get(entry_id)['check_message']


def test_replacement_failure_rolls_back_original_material(api, tmp_path, monkeypatch):
    entry_id, old = existing_entry(api, tmp_path)
    before = Path(old['abs_path']).read_bytes()
    new = make_pdf(tmp_path / 'new.pdf', 1, '2026-10-30 10:00:00')

    def fail(_):
        raise RuntimeError('simulated database failure')

    monkeypatch.setattr(api.attachments, '_touch_entry', fail)
    result = api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old]))
    assert not result['ok']
    assert Path(old['abs_path']).read_bytes() == before
    assert api.attachments.get(old['id'])['sha256'] == old['sha256']
    assert len(list(Path(old['abs_path']).parent.glob('*.pdf'))) == 1


@pytest.mark.parametrize('action', ['replace', 'keep', 'cancel'])
def test_automatic_save_waits_for_confirmation_before_archiving_or_cleanup(api, tmp_path, action):
    entry_id, old = existing_entry(api, tmp_path)
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    output = make_pdf(Path(directory.name) / 'automatic.pdf', 1, '2026-10-30 10:00:00')
    session = {'id': 'confirmation', 'entry_id': entry_id, 'invoice_no': NUMBER,
               'auto_save_dir': directory, 'auto_save_path': output,
               'window': None, 'window_closed': False, 'attached': None,
               'watch_directories': [], 'before': {}, 'started_ns': time.time_ns(),
               'candidate_sizes': {}, 'seen_candidates': set(), 'last_message': '',
               'trash_source_after_archive': True}
    api._verification_sessions['confirmation'] = session
    assert api.invoice_verification_status('confirmation')['data']['state'] == 'processing'
    pending = api.invoice_verification_status('confirmation')
    assert pending['code'] == 'inspection_conflict'
    assert output.exists()
    assert not session['seen_candidates']
    assert len(api.attachments.list(entry_id)) == 1
    if action == 'cancel':
        api.close_invoice_verification('confirmation')
        assert Path(old['abs_path']).is_file()
    else:
        result = api.invoice_verification_status('confirmation', decision([old], action))
        assert result['ok'], result
        assert result['data']['state'] == 'attached'
        assert result['data']['source_trashed'] is False
        assert len(api.attachments.list(entry_id)) == (1 if action == 'replace' else 2)
    assert not output.exists()


def test_conflict_during_automatic_save_brings_main_window_forward(api, tmp_path, monkeypatch):
    """确认框在主窗口里，查验窗口盖住它时必须把主窗口提到前面，否则用户看不到。"""
    from tidoc import api_verification

    entry_id, _ = existing_entry(api, tmp_path)
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    output = make_pdf(Path(directory.name) / 'automatic.pdf', 1, '2026-10-30 10:00:00')
    session = {'id': 'front', 'entry_id': entry_id, 'invoice_no': NUMBER,
               'auto_save_dir': directory, 'auto_save_path': output,
               'window': None, 'window_closed': False, 'attached': None,
               'watch_directories': [], 'before': {}, 'started_ns': time.time_ns(),
               'candidate_sizes': {}, 'seen_candidates': set(), 'last_message': '',
               'trash_source_after_archive': False}
    api._verification_sessions['front'] = session
    main_window = object()
    api._window = main_window
    activated = []
    monkeypatch.setattr(api_verification, 'activate_main_window', activated.append)
    api.invoice_verification_status('front')
    assert api.invoice_verification_status('front')['code'] == 'inspection_conflict'
    assert activated == [main_window]


def test_startup_checks_existing_multiple_materials_without_reparsing_cached_pdfs(api, tmp_path, monkeypatch):
    from tidoc.api import Api
    from tidoc.services import inspection_pdf

    entry_id, old = existing_entry(api, tmp_path)
    new = make_pdf(tmp_path / 'extra.pdf', 1, '2026-10-30 10:00:00')
    assert api.add_attachment(entry_id, str(new), 'inspection_pdf', options=decision([old], 'keep'))['ok']
    api.db.conn.execute("UPDATE entries SET check_message='',check_status='pass',status_engine_version='adapter-policy-1' WHERE id=?", (entry_id,))
    api.db.conn.commit()
    monkeypatch.setattr(inspection_pdf, 'read_inspection_pdf', lambda _: pytest.fail('should reuse PDF cache'))
    fresh = Api(api.data_root.root)
    assert '2 份查验单' in fresh.entries.get(entry_id)['check_message']
    assert fresh.entries.get(entry_id)['check_status'] == 'warning'
    fresh.db.close()


def automatic_session(api, tmp_path, entry_id):
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    output = make_pdf(Path(directory.name) / 'automatic.pdf', 1, '2026-10-30 10:00:00')
    session = {'id': 'retry', 'entry_id': entry_id, 'invoice_no': NUMBER,
               'auto_save_dir': directory, 'auto_save_path': output,
               'window': None, 'window_closed': False, 'attached': None,
               'watch_directories': [], 'before': {}, 'started_ns': time.time_ns(),
               'candidate_sizes': {}, 'seen_candidates': set(), 'last_message': '',
               'trash_source_after_archive': False}
    api._verification_sessions['retry'] = session
    return session, output


def test_persistent_save_errors_stop_waiting_and_report_the_reason(api, tmp_path, monkeypatch):
    from tidoc.api_verification import VERIFICATION_RETRY_LIMIT

    entry_id = new_entry(api)
    session, output = automatic_session(api, tmp_path, entry_id)
    calls = []

    def disk_full(*args, **kwargs):
        calls.append(args)
        raise OSError('No space left on device')

    monkeypatch.setattr(api.attachments, 'add', disk_full)
    # 第一轮只确认文件大小稳定，之后每轮重试一次。
    for _ in range(VERIFICATION_RETRY_LIMIT + 1):
        assert api.invoice_verification_status('retry')['data']['state'] == 'processing'
    result = api.invoice_verification_status('retry')['data']
    assert result['state'] == 'waiting'
    assert '未能绑定' in result['message'] and 'No space left on device' in result['message']
    assert len(calls) == VERIFICATION_RETRY_LIMIT + 1
    assert api.invoice_verification_status('retry')['data']['state'] == 'waiting'
    assert len(calls) == VERIFICATION_RETRY_LIMIT + 1
    assert output.exists() and not api.attachments.list(entry_id)


def test_transient_save_error_is_retried_until_the_file_can_be_archived(api, tmp_path, monkeypatch):
    entry_id = new_entry(api)
    session, output = automatic_session(api, tmp_path, entry_id)
    original, attempts = api.attachments.add, []

    def flaky(*args, **kwargs):
        attempts.append(args)
        if len(attempts) < 3:
            raise PermissionError('The PDF is still open in the printer')
        return original(*args, **kwargs)

    monkeypatch.setattr(api.attachments, 'add', flaky)
    for _ in range(3):
        assert api.invoice_verification_status('retry')['data']['state'] == 'processing'
    result = api.invoice_verification_status('retry')['data']
    assert result['state'] == 'attached'
    assert len(api.attachments.list(entry_id)) == 1
    assert not output.exists()
