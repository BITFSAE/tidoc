"""Settings reads must stay small and must not queue behind directory scans."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event


def test_settings_snapshot_reads_latest_preferences_without_schemes_or_scans(api, monkeypatch):
    import tidoc.api as api_module
    def unexpected(*args, **kwargs):
        raise AssertionError('Settings snapshot should not inspect schemes or directories')
    monkeypatch.setattr(api.adapters, 'get_scheme', unexpected)
    monkeypatch.setattr(api_module, '_directory_size', unexpected)
    monkeypatch.setattr(api, '_cache_cleanup_candidates', unexpected)
    api._set_preference_value('tidoc.multiClaimantMode', '1')
    api._set_preference_value('tidoc.update.autoCheck', '0')
    api._set_preference_value('tidoc.update.channel', 'beta')
    result = api.settings_data()
    assert result['ok'], result
    data = result['data']
    assert data['paths']['root'] == str(api.data_root.root)
    assert data['app_info'] == api.app_info()['data']
    assert data['preferences']['tidoc.multiClaimantMode'] == '1'
    assert data['preferences']['tidoc.update.autoCheck'] == '0'
    assert data['channel'] == 'beta'
    api._set_preference_value('tidoc.update.channel', 'stable')
    assert api.settings_data()['data']['channel'] == 'stable'


def test_maintenance_scan_does_not_hold_api_lock(api, monkeypatch):
    import tidoc.api as api_module
    started, release = Event(), Event()
    def scan(folder):
        started.set()
        assert release.wait(5)
        return 123
    monkeypatch.setattr(api_module, '_directory_size', scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan_result = pool.submit(api.storage_maintenance_status)
        try:
            assert started.wait(2)
            # This guarded bridge call must complete while scanning remains blocked.
            assert pool.submit(api.app_info).result(timeout=2)['ok']
        finally:
            release.set()
        assert scan_result.result(timeout=2)['data']['exports_size'] == 123
