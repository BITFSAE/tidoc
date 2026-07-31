import json
from pathlib import Path

from tidoc import __version__
from tidoc.api import APP_LAST_SEEN_VERSION_KEY, AUTO_UPDATE_PREF_KEY, Api
from tidoc.services.updater import current_platform, sha256_file


def unwrap(result):
    assert result["ok"] is True
    return result["data"]


def test_auto_update_check_is_opt_in_and_throttled(monkeypatch, tmp_path):
    from tidoc.services import updater

    api = Api(tmp_path)
    calls = []

    def fake_check(*args, **kwargs):
        calls.append(1)
        return {"current_core_version": __version__, "updates": []}

    monkeypatch.setattr(updater, "check_updates", fake_check)
    disabled = unwrap(api.auto_check_updates())
    assert disabled["reason"] == "disabled"
    assert calls == []

    unwrap(api.set_app_preference(AUTO_UPDATE_PREF_KEY, "1"))
    first = unwrap(api.auto_check_updates())
    second = unwrap(api.auto_check_updates())
    assert first["checked"] is True
    assert second["reason"] == "recent"
    assert len(calls) == 1


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


def test_cleanup_removes_only_rebuildable_files(tmp_path):
    api = Api(tmp_path)
    dropped = api.data_root.dropped_dir / "drop" / "temp.pdf"
    dropped.parent.mkdir(parents=True)
    dropped.write_bytes(b"drop")
    stale = api.data_root.updates_dir / "old.exe"
    stale.write_bytes(b"old")

    pending = api.data_root.updates_dir / "pending.exe"
    pending.write_bytes(b"pending")
    marker = api.data_root.updates_dir / "core" / current_platform() / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "version": "9.9.9",
        "file_path": str(pending),
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
    assert marker.exists()
    assert exported.exists()
    assert not dropped.exists()
    assert not stale.exists()


def test_app_info_has_manual_release_link(tmp_path):
    info = unwrap(Api(tmp_path).app_info())
    assert info["releases"].endswith("/releases/latest")
