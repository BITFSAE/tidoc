import json
from pathlib import Path

from tidoc import __version__
from tidoc.api import (
    APP_LAST_SEEN_VERSION_KEY,
    AUTO_UPDATE_INTERVAL_SECONDS,
    AUTO_UPDATE_PREF_KEY,
    Api,
)
from tidoc.services.updater import current_platform, sha256_file


def unwrap(result):
    assert result["ok"] is True
    return result["data"]


def test_auto_update_check_is_enabled_by_default_can_be_disabled_and_is_throttled(monkeypatch, tmp_path):
    from tidoc.services import updater

    api = Api(tmp_path)
    calls = []

    def fake_check(*args, **kwargs):
        calls.append(1)
        return {"current_core_version": __version__, "updates": []}

    monkeypatch.setattr(updater, "check_updates", fake_check)
    first = unwrap(api.auto_check_updates())
    second = unwrap(api.auto_check_updates())
    assert first["checked"] is True
    assert second["reason"] == "recent"
    assert len(calls) == 1

    assert AUTO_UPDATE_INTERVAL_SECONDS == 60 * 60

    unwrap(api.set_app_preference(AUTO_UPDATE_PREF_KEY, "0"))
    disabled = unwrap(api.auto_check_updates())
    assert disabled["reason"] == "disabled"
    assert len(calls) == 1


def test_cached_core_update_is_normalized_against_running_version(tmp_path):
    api = Api(tmp_path)
    api._record_update_check({
        "current_core_version": "0.0.1",
        "updates": [{
            "component": "core",
            "current_version": "0.0.1",
            "latest_version": __version__,
            "available": True,
            "downloaded": True,
            "state": "downloaded",
        }],
    })

    cached = api._cached_update_result()

    assert cached["current_core_version"] == __version__
    assert cached["updates"][0]["available"] is False
    assert cached["updates"][0]["state"] == "current"
    assert cached["updates"][0]["downloaded"] is False


def test_auto_check_failure_keeps_cached_update_and_retries_soon(monkeypatch, tmp_path):
    from tidoc.services import updater

    api = Api(tmp_path)
    api._record_update_check({
        "updates": [{
            "component": "core",
            "latest_version": "9.9.9",
            "available": True,
            "asset": {"version": "9.9.9"},
        }]
    }, checked_at=1)
    monkeypatch.setattr(updater, "check_updates", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("网络暂不可用")))

    status = unwrap(api.auto_check_updates())

    assert status["reason"] == "error"
    assert status["error"] == "网络暂不可用"
    assert status["updates"][0]["available"] is True
    assert status["next_check_at"] > status["checked_at"]


def test_startup_update_state_only_announces_real_upgrade(tmp_path):
    api = Api(tmp_path)
    first = unwrap(api.startup_update_state())
    assert first["first_launch"] is True
    assert first["upgraded"] is False

    unwrap(api.set_app_preference(APP_LAST_SEEN_VERSION_KEY, "0.0.1"))
    api._record_update_check({
        "updates": [{
            "component": "core",
            "latest_version": __version__,
            "asset": {"notes": ["新的更新体验"]},
        }]
    })
    upgraded = unwrap(api.startup_update_state())
    assert upgraded["upgraded"] is True
    assert upgraded["notes"] == ["新的更新体验"]
    assert unwrap(api.startup_update_state())["upgraded"] is False


def test_cleanup_removes_only_rebuildable_files(monkeypatch, tmp_path):
    api = Api(tmp_path)
    dropped = api.data_root.dropped_dir / "drop" / "temp.pdf"
    dropped.parent.mkdir(parents=True)
    dropped.write_bytes(b"drop")
    stale = api.data_root.updates_dir / "old.exe"
    stale.write_bytes(b"old")

    pending = api.data_root.updates_dir / "pending.exe"
    pending.write_bytes(b"pending")
    staged = api.data_root.updates_dir / "core" / current_platform() / "9.9.9" / "staged" / "tidoc"
    staged.mkdir(parents=True)
    staged_file = staged / "tidoc.exe"
    staged_file.write_bytes(b"staged")
    marker = api.data_root.updates_dir / "core" / current_platform() / "current.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "version": "9.9.9",
        "file_path": str(pending),
        "stage_dir": str(staged),
        "package_kind": "silent",
        "sha256": sha256_file(pending),
    }), "utf-8")

    exported = api.data_root.exports_dir / "nested" / "材料.tidoc"
    exported.parent.mkdir(parents=True)
    exported.write_bytes(b"exported")

    status = unwrap(api.storage_maintenance_status())
    assert status["files"] == 2
    assert status["exports_size"] == len(b"exported")
    cleaned = unwrap(api.cleanup_app_cache())
    assert cleaned["files"] == 2
    assert pending.exists()
    assert staged_file.exists()
    assert marker.exists()
    assert exported.exists()
    assert not dropped.exists()
    assert not stale.exists()

    # A legacy/manual pending marker has no stage_dir; an empty value must not
    # accidentally preserve every update file below the current directory.
    marker.write_text(json.dumps({
        "version": "9.9.9",
        "file_path": str(pending),
        "sha256": sha256_file(pending),
    }), "utf-8")
    stale_again = api.data_root.updates_dir / "old-again.exe"
    stale_again.write_bytes(b"old-again")
    monkeypatch.chdir(tmp_path)
    cleaned_again = unwrap(api.cleanup_app_cache())
    assert cleaned_again["files"] == 2
    assert pending.exists()
    assert not stale_again.exists()


def test_app_info_has_manual_release_link(tmp_path):
    info = unwrap(Api(tmp_path).app_info())
    assert info["releases"].endswith("/releases/latest")


def test_frontend_health_marker_is_written_atomically(tmp_path):
    health = tmp_path / "handoff" / "health.json"
    api = Api(tmp_path / "data", update_health_path=health)

    result = unwrap(api.mark_frontend_ready())
    payload = json.loads(health.read_text("utf-8"))

    assert result["required"] is True
    assert payload["version"] == __version__
    assert payload["pid"] > 0
    assert not list(health.parent.glob(".health.json-*.tmp"))


def test_frontend_upgrade_confirmation_shows_version_transition():
    source = (
        Path(__file__).resolve().parents[1] / "tidoc" / "web" / "app.js"
    ).read_text("utf-8")

    assert "showCompletedUpdateWhenFree(startupUpdate)" in source
    assert "openReleaseHighlights('completed', data)" in source
    assert "setTimeout(() => { maybeAutoCheckUpdates(); }, 250)" in source
    assert "function scheduleAutoUpdateCheck(status)" in source
    assert "next_check_at" in source
    assert "state: 'downloading'" in source
    assert "progress: 0" in source


def test_core_download_reuses_cached_asset(monkeypatch, tmp_path):
    from tidoc.services import updater

    api = Api(tmp_path)
    asset = {
        "component": "core",
        "version": "9.9.9",
        "auto_update": {"url": "https://example.com/update.zip"},
    }
    api._record_update_check({
        "updates": [{
            "component": "core",
            "latest_version": "9.9.9",
            "available": True,
            "asset": asset,
        }]
    })
    captured = []
    monkeypatch.setattr(api._core_updater, "start", lambda value: captured.append(value) or {"state": "downloading"})
    monkeypatch.setattr(updater, "load_manifest", lambda: (_ for _ in ()).throw(AssertionError("不应重新读取清单")))

    result = unwrap(api.start_core_update_download())

    assert result["state"] == "downloading"
    assert captured == [asset]
