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


def test_print_component_status_does_not_hold_api_lock(api, monkeypatch):
    import tidoc.services.printing as printing
    started, release = Event(), Event()
    def probe(components_dir=None):
        started.set()
        assert release.wait(5)
        return {'available': False}
    monkeypatch.setattr(printing, 'component_status', probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        status = pool.submit(api.print_component_status)
        try:
            assert started.wait(2)
            assert pool.submit(api.app_info).result(timeout=2)['ok']
        finally:
            release.set()
        assert status.result(timeout=2) == {'ok': True, 'data': {'available': False}}


def test_component_hash_is_reused_until_the_file_changes(tmp_path, monkeypatch):
    import os
    from tidoc.services import updater
    exe = tmp_path / 'tidoc_print'
    exe.write_bytes(b'first')
    calls = []
    real = updater.sha256_file
    monkeypatch.setattr(updater, 'sha256_file', lambda path: calls.append(path) or real(path))
    first = updater.cached_sha256_file(exe)
    assert updater.cached_sha256_file(exe) == first and len(calls) == 1
    exe.write_bytes(b'second')
    os.utime(exe, ns=(exe.stat().st_atime_ns, exe.stat().st_mtime_ns + 1_000_000))
    assert updater.cached_sha256_file(exe) != first and len(calls) == 2


def test_print_capabilities_persist_across_launches_but_timeouts_do_not(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tidoc.services import printing
    exe = tmp_path / 'tidoc_print'
    exe.write_bytes(b'component')
    cache = tmp_path / 'capabilities-cache.json'
    runs = []
    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        if len(runs) == 1:
            raise subprocess.TimeoutExpired(cmd, 10)
        if len(runs) == 2:
            return SimpleNamespace(returncode=0, stdout='{"renderers": ["docx"]}', stderr='')
        return SimpleNamespace(returncode=2, stdout='', stderr='unrecognized arguments: --capabilities')
    monkeypatch.setattr(printing.subprocess, 'run', fake_run)
    printing._CAPABILITIES_CACHE.clear()
    assert printing._query_capabilities(exe, cache) == {}
    assert not cache.exists()  # A timeout may be transient; the next launch probes again.
    printing._CAPABILITIES_CACHE.clear()
    assert printing._query_capabilities(exe, cache) == {'renderers': ['docx']}
    printing._CAPABILITIES_CACHE.clear()  # Simulate a new launch.
    assert printing._query_capabilities(exe, cache) == {'renderers': ['docx']}
    assert len(runs) == 2
    # A replaced executable is probed again; an old component that rejects the
    # option gives a definite answer, which is remembered as well.
    exe.write_bytes(b'older component')
    printing._CAPABILITIES_CACHE.clear()
    assert printing._query_capabilities(exe, cache) == {}
    printing._CAPABILITIES_CACHE.clear()
    assert printing._query_capabilities(exe, cache) == {}
    assert len(runs) == 3
