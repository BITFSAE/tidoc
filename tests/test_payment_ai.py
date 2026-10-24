"""Vision integration: opt-in, billing, concurrency, cache, and material safety."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError

from PIL import Image
import pytest

from tidoc.services import payment_ai as ai
from tidoc.engine.models import ParsedInvoice
from tests.conftest import enable_payment_ai, payment_result


def cfg(**changes):
    return ai.validate_config({**ai.DEFAULT_CONFIG, 'enabled': True, **changes})


def image(tmp_path, name='receipt.png', color='white'):
    path = tmp_path / name
    Image.new('RGB', (320, 640), color).save(path)
    return path


def raw(**changes):
    return dict(dict(source='bank', kind='receipt', paid='43.20', refund=None,
                     completed=True, currency='CNY', theme='light', issue=''), **changes)


def complete(value=None, **extra):
    return dict(result=value or raw(), usage={'prompt_tokens': 1000, 'completion_tokens': 100},
                resolved_model='test', **extra)


def wait(api, job):
    limit = time.monotonic()+8
    while time.monotonic() < limit:
        result = api.payment_ai_progress(job['id'])['data']
        if result['done']:
            return result
        time.sleep(.01)
    raise AssertionError('job did not finish')


@pytest.mark.parametrize('beijing,period', [
    ('2026-10-26T08:59', '低谷'), ('2026-10-26T09:00', '高峰'),
    ('2026-10-26T11:59', '高峰'), ('2026-10-26T12:00', '低谷'),
    ('2026-10-26T14:00', '高峰'), ('2026-10-26T18:00', '低谷'),
    ('2026-10-24T10:00', '低谷'), ('2026-10-01T10:00', '低谷'),
    ('2026-10-10T10:00', '低谷'),  # adjusted working Saturday still off-peak
])
def test_pricing_uses_beijing_boundaries_weekends_and_holidays(beijing, period):
    date = datetime.fromisoformat(beijing).replace(tzinfo=ai.BEIJING)
    info = ai.price_info(cfg(), date.astimezone(timezone.utc))
    assert info['period'] == period
    assert info['input'] == (2 if period == '高峰' else 1)


def test_cost_uses_cached_input_and_does_not_price_unknown_provider_as_deepseek():
    rates = ai.price_info(cfg(), datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert ai.usage_cost({'prompt_tokens': 1000000, 'prompt_cache_hit_tokens': 500000,
                          'completion_tokens': 100000}, rates) == .91
    assert not ai.price_info(cfg(base_url='https://example.com'))['known']
    assert not ai.price_info(cfg(model='another-model'))['known']
    assert ai.price_info(cfg(provider='qwen', **ai.PRESETS['qwen']))['input'] == .8
    assert not ai.price_info(cfg(), datetime(2028, 1, 1, tzinfo=timezone.utc))['calendar_known']


@pytest.mark.parametrize('change', [{'concurrency': 0}, {'concurrency': 1.5}, {'base_url': 'http://example.com'},
    {'base_url': 'https://key@example.com'}, {'base_url': 'https://example.com?key=secret'},
    {'input_price': float('nan')}, {'output_price': -1}])
def test_invalid_configuration_is_rejected(change):
    with pytest.raises(ValueError): cfg(**change)


def test_old_ocr_switch_and_environment_keys_never_opt_user_into_network(api, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'environment-only-key')
    api.set_app_preference('tidoc.paymentScreenshotOcr', '1')
    assert not api._payment_ai_enabled()
    assert not api.payment_ai_settings()['data']['enabled']
    assert not api.start_payment_ai([])['ok']


def test_key_is_masked_and_scoped_to_endpoint_and_provider(api):
    enable_payment_ai(api)
    snapshot = api.payment_ai_settings()['data']
    assert snapshot['key_configured'] and 'test-key' not in json.dumps(snapshot)
    result = api.save_payment_ai_settings({'base_url': 'https://example.com'})
    assert not result['ok'] and 'Key' in result['error']
    assert api._payment_ai_config()['base_url'] == 'https://api.deepseek.com'
    assert api.save_payment_ai_settings({'enabled': False}, clear_key=True)['ok']
    assert not api._payment_ai_key(api._payment_ai_config())


def test_client_cache_hashes_content_config_and_key_and_does_not_modify_images(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ai, 'request_completion', lambda *args: calls.append(args) or complete())
    path = image(tmp_path); before = path.read_bytes()
    client = ai.PaymentClient(tmp_path/'cache')
    first = client.recognize(path, cfg(), 'secret')
    second = client.recognize(path, cfg(concurrency=2), 'secret')
    assert not first['cached'] and second['cached'] and second['cost'] == 0
    client.recognize(path, cfg(requirements='需有付款时间'), 'secret')
    client.recognize(path, cfg(), 'new-secret')
    assert len(calls) == 3 and path.read_bytes() == before
    assert 'secret' not in ''.join(p.read_text('utf-8') for p in (tmp_path/'cache').glob('*.json'))
    assert calls[0][2]['thinking'] == {'type': 'disabled'}
    assert calls[0][2]['stream']


def test_failed_schema_is_not_cached_and_auth_error_never_leaks_body(monkeypatch, tmp_path):
    path = image(tmp_path); client = ai.PaymentClient(tmp_path/'cache')
    monkeypatch.setattr(ai, 'request_completion', lambda *args: complete({'secret': 'provider-echo'}))
    with pytest.raises(ValueError, match='格式'): client.recognize(path, cfg(), 'secret')
    assert not list((tmp_path/'cache').glob('*.json'))
    count = []
    def fail(*args):
        count.append(1)
        raise HTTPError('https://example.com', 401, 'echo secret', {}, None)
    monkeypatch.setattr(ai, 'request_completion', fail)
    with pytest.raises(ValueError, match='Key 无效') as exc:
        client.recognize(path, cfg(), 'secret')
    assert len(count) == 1 and 'secret' not in str(exc.value)


def test_job_concurrency_progress_and_settings_do_not_block_each_other(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    assert api.save_payment_ai_settings({'concurrency': 2})['ok']
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock(); active = peak = 0
    def request(*args):
        nonlocal active, peak
        with lock:
            active += 1; peak = max(peak, active)
            if active == 2: started.set()
        assert release.wait(5)
        with lock: active -= 1
        return complete()
    monkeypatch.setattr(ai, 'request_completion', request)
    paths = [str(image(tmp_path, f'{i}.png', color)) for i, color in enumerate(['white', 'red', 'blue'])]
    job = api.start_payment_ai(paths)['data']
    try:
        assert started.wait(3)
        state = api.payment_ai_progress(job['id'])['data']
        assert state['completed'] == 0 and state['active'] == 2 and state['queued'] == 1
        with ThreadPoolExecutor() as pool:
            assert pool.submit(api.settings_data).result(timeout=1)['ok']
    finally: release.set()
    state = wait(api, job)
    assert peak == 2 and state['completed'] == 3 and state['recognized'] == 3
    assert api.payment_ai_settings()['data']['total_calls'] == 3
    assert 'test-key' not in json.dumps(state)
    cached = wait(api, api.start_payment_ai(paths)['data'])
    assert cached['cached'] == 3 and cached['cost'] == 0
    assert api.payment_ai_settings()['data']['total_calls'] == 3


def test_net_refund_review_and_dark_advisory_survive_attachment_save(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    monkeypatch.setattr(ai, 'request_completion', lambda *args: complete(raw(refund='16.20', theme='dark')))
    path = image(tmp_path)
    profile = api.create_profile('测试人员', '测试审核人')['data']
    eid = api.entries.create(profile['id'], parsed=ParsedInvoice(total=Decimal('27.00')))
    job = wait(api, api.start_payment_ai([str(path)])['data'])
    att = api.add_attachment(eid, str(path), 'payment_screenshot', options={'payment_ai_job_id': job['id']})['data']
    assert att['recognized_value'] == '27.00' and att['recognition_status'] == 'review'
    assert '退款' in att['recognition_message'] and '浅色' in att['recognition_message']
    refreshed = api.entries.get(eid)
    assert '退款' in refreshed['check_message']
    assert refreshed['fields']['paid_amount']['value_source'] != 'payment_ai'
    assert api.payment_ai_settings()['data']['total_calls'] == 1


def test_job_never_applies_result_to_replaced_or_deleted_material(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    person = api.create_profile('测试人员', '测试审核人')['data']
    eid = api.entries.create(person['id'])
    path = image(tmp_path)
    att = api.attachments.add(eid, path, 'payment_screenshot')
    entered, release = threading.Event(), threading.Event()
    def request(*args):
        entered.set(); assert release.wait(5); return complete()
    monkeypatch.setattr(ai, 'request_completion', request)
    job = api.start_payment_ai(entry_ids=[eid])['data']
    try:
        assert entered.wait(3)
        # Delete while the API is in flight, without waiting for the provider.
        assert api.delete_attachment(att['id'])['ok']
    finally: release.set()
    assert wait(api, job)['done']
    assert not api.attachments.list(eid)


def test_payment_system_ocr_path_was_removed():
    from tidoc.services import folder_import
    assert not hasattr(folder_import, 'extract_payment_image_amount')
    assert not hasattr(folder_import, '_payment_amount_from_text')
    assert not hasattr(folder_import, '_windows_ocr_image_text')
    assert not hasattr(folder_import, '_macos_ocr_image_text')


def test_manual_confirmation_even_at_invoice_total_is_never_overwritten(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    person = api.create_profile('测试人员', '测试审核人')['data']
    eid = api.entries.create(person['id'], parsed=ParsedInvoice(total=Decimal('43.20')))
    api.update_field(eid, 'paid_amount', '43.20')
    assert api.entries.get(eid)['fields']['paid_amount']['value_source'] == 'manual'
    monkeypatch.setattr(api, '_payment_ai_cached', lambda path: payment_result('27.00'))
    api.add_attachment(eid, str(image(tmp_path)), 'payment_screenshot')
    assert api.entries.get(eid)['fields']['paid_amount']['current'] == '43.20'


def test_failed_prepared_result_is_reused_without_sending_image_again(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    calls = []
    def request(*args):
        calls.append(1)
        raise HTTPError('https://example.com', 401, 'secret', {}, None)
    monkeypatch.setattr(ai, 'request_completion', request)
    path = image(tmp_path)
    job = wait(api, api.start_payment_ai([str(path)])['data'])
    assert job['failed'] == 1
    person = api.create_profile('测试人员', '测试审核人')['data']
    eid = api.entries.create(person['id'])
    att = api.add_attachment(eid, str(path), 'payment_screenshot', options={'payment_ai_job_id': job['id']})['data']
    assert att['recognition_status'] == 'error' and len(calls) == 1


def test_disabling_stops_queued_uploads_and_migration_waits_for_jobs(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    api.save_payment_ai_settings({'concurrency': 1})
    entered, release = threading.Event(), threading.Event()
    calls = []
    def request(*args):
        calls.append(1); entered.set(); assert release.wait(5); return complete()
    monkeypatch.setattr(ai, 'request_completion', request)
    paths = [str(image(tmp_path, 'first.png')), str(image(tmp_path, 'second.png', 'blue'))]
    job = api.start_payment_ai(paths)['data']
    try:
        assert entered.wait(2)
        with pytest.raises(ValueError, match='迁移'): api._ensure_payment_ai_idle()
        assert not api.save_payment_ai_settings({'concurrency': 4})['ok']
        assert api.save_payment_ai_settings({'enabled': False})['ok']
    finally: release.set()
    assert wait(api, job)['cancelled'] and len(calls) == 1
    api._ensure_payment_ai_idle()


def test_disabled_recognition_never_asks_to_rerun_screenshots(api, tmp_path):
    person = api.create_profile('测试人员', '测试审核人')['data']
    eid = api.entries.create(person['id'], parsed=ParsedInvoice(total=Decimal('27.00')))
    att = api.add_attachment(eid, str(image(tmp_path)), 'payment_screenshot')['data']
    api.add_attachment(eid, str(image(tmp_path, 'b.png')), 'payment_screenshot')
    api.update_attachment(att['id'], {'note': '补充'})
    api.delete_attachment(att['id'])
    assert '付款截图' not in api.entries.get(eid)['check_message']


def test_attachment_paths_reuse_job_result_and_never_upload_under_api_lock(api, monkeypatch, tmp_path):
    from urllib.error import URLError
    enable_payment_ai(api)
    calls = []
    def request(*args):
        calls.append(1); raise URLError('offline')
    monkeypatch.setattr(ai, 'request_completion', request)
    monkeypatch.setattr(ai.time, 'sleep', lambda seconds: None)
    path = image(tmp_path)
    job = wait(api, api.start_payment_ai([str(path)])['data'])
    sent = len(calls)
    person = api.create_profile('测试人员', '测试审核人')['data']
    created = api.create_entry(person['id'], payment_paths=[str(path)], payment_ai_job_id=job['id'])['data']
    other = api.entries.create(person['id'])
    api.add_attachment(other, str(image(tmp_path, 'plain.png', 'gray')), 'other')
    plain = api.attachments.list(other)[0]
    api.update_attachment(plain['id'], {'type': 'payment_screenshot'})
    assert len(calls) == sent
    att = next(a for a in created['attachments'] if a['type'] == 'payment_screenshot')
    assert att['recognition_status'] == 'error'
    assert '尚未按当前规则识别' in api.entries.get(other)['check_message']


def test_progress_sends_results_once_and_cancel_is_not_a_failure(api, monkeypatch, tmp_path):
    enable_payment_ai(api)
    entered, release = threading.Event(), threading.Event()
    def request(*args):
        entered.set(); assert release.wait(5); return complete()
    monkeypatch.setattr(ai, 'request_completion', request)
    api.save_payment_ai_settings({'concurrency': 1})
    paths = [str(image(tmp_path, f'{i}.png', (i, i, i))) for i in range(3)]
    job = api.start_payment_ai(paths)['data']
    assert entered.wait(3)
    running = api.payment_ai_progress(job['id'])['data']
    assert running['results'] == [] and not running['done']
    api.cancel_payment_ai(job['id'])
    release.set()
    state = wait(api, job)
    assert len(state['results']) == 3
    assert state['cancelled_count'] == 2 and state['failed'] == 0


def test_certificate_failure_is_reported_without_retry(monkeypatch, tmp_path):
    import ssl
    from urllib.error import URLError
    calls = []
    def request(*args):
        calls.append(1); raise URLError(ssl.SSLCertVerificationError('verify failed'))
    monkeypatch.setattr(ai, 'request_completion', request)
    client = ai.PaymentClient(tmp_path / 'cache')
    with pytest.raises(ValueError, match='证书'):
        client.recognize(image(tmp_path), cfg(), 'key')
    assert calls == [1]


def test_macos_context_adds_system_roots(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    loaded = []
    class Context:
        def load_verify_locations(self, cadata=None): loaded.append(cadata)
    monkeypatch.setattr(ai, '_SSL_CONTEXT', None)
    monkeypatch.setattr(ai.sys, 'platform', 'darwin')
    monkeypatch.setattr(ai.ssl, 'create_default_context', Context)
    monkeypatch.setattr(ai.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='-----BEGIN CERTIFICATE-----x'))
    assert isinstance(ai.ssl_context(), Context) and loaded == ['-----BEGIN CERTIFICATE-----x']
    monkeypatch.setattr(ai, '_SSL_CONTEXT', None)
