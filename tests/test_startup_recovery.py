"""Newer data must remain untouched by startup and rollback recovery."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from tidoc import app
from tidoc.db.database import Database
from tidoc.db.schema import DatabaseVersionError, SCHEMA_VERSION
from tidoc.services import startup


def _write_version(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(value),))
        conn.commit()


def test_compatibility_probe_is_read_only_and_honors_relocated_data(monkeypatch, tmp_path):
    root = tmp_path / "数据 空间"
    root.mkdir()
    path = root / "tidoc.sqlite"
    _write_version(path, SCHEMA_VERSION + 1)
    before = path.read_bytes()
    monkeypatch.setattr(startup, "resolve_data_root", lambda: root)
    monkeypatch.setattr(startup, "SingleInstance", lambda: app.SingleInstance(tmp_path / "runtime"))
    assert startup.check_data_compatibility() is False
    assert path.read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == ["tidoc.sqlite"]
    with pytest.raises(DatabaseVersionError) as error:
        Database(path)
    assert error.value.actual == SCHEMA_VERSION + 1
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE meta SET value=?", (str(SCHEMA_VERSION),))
        conn.commit()
    assert startup.check_data_compatibility() is True
    path.unlink()  # Probe and rejected startup must release file handles on Windows too.


def test_probe_refuses_running_instance_and_invalid_version(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "resolve_data_root", lambda: tmp_path)
    monkeypatch.setattr(startup, "SingleInstance", lambda: app.SingleInstance(tmp_path / "runtime"))
    with app.SingleInstance(tmp_path / "runtime"):
        assert startup.check_data_compatibility() is False
    assert startup.check_data_compatibility() is True
    assert not (tmp_path / "tidoc.sqlite").exists()
    _write_version(tmp_path / "tidoc.sqlite", "bad")
    with pytest.raises(ValueError, match="数据库版本无效"):
        startup.check_data_compatibility()


@pytest.mark.parametrize("automatic", [False, True])
def test_startup_failure_shows_help_or_exits_for_installer(monkeypatch, tmp_path, automatic):
    seen = []
    app_singleton = SimpleNamespace(
        acquire=lambda: True,
        release=lambda: seen.append("released"),
    )
    monkeypatch.setattr(app, "SingleInstance", lambda: app_singleton)
    monkeypatch.setattr(app, "_install_native_stderr_filter", lambda: None)
    monkeypatch.setattr(app, "_migrate_windows_file_icon", lambda: None)
    error = DatabaseVersionError(SCHEMA_VERSION + 1, SCHEMA_VERSION)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(app, "Api", fail)
    monkeypatch.setattr(startup, "show_startup_error", lambda exc, **kwargs: seen.append(exc))
    argv = ["tidoc"]
    if automatic:
        argv += ["--update-health-file", str(tmp_path / "health.json")]
    monkeypatch.setattr(app.sys, "argv", argv)
    if automatic:
        with pytest.raises(SystemExit) as exit_info:
            app.main()
        assert exit_info.value.code == 1
        assert seen == ["released"]
    else:
        app.main()
        assert seen == [error, "released"]


def test_recovery_help_never_opens_database(monkeypatch):
    seen = []
    monkeypatch.setattr(app.sys, "argv", ["tidoc", "--update-recovery"])
    monkeypatch.setattr(app, "SingleInstance", lambda: SimpleNamespace(
        acquire=lambda: True, release=lambda: None,
    ))
    monkeypatch.setattr(app, "_install_native_stderr_filter", lambda: None)
    monkeypatch.setattr(app, "_migrate_windows_file_icon", lambda: None)
    monkeypatch.setattr(app, "Api", lambda *a, **kw: pytest.fail("recovery must not open data"))
    monkeypatch.setattr(startup, "show_startup_error", lambda exc=None, **kw: seen.append(kw))
    app.main()
    assert seen == [{"update_recovery": True}]


def test_broken_help_window_exits_nonzero_instead_of_stacking_dialogs(monkeypatch):
    released = []
    monkeypatch.setattr(app.sys, "argv", ["tidoc"])
    monkeypatch.setattr(app, "SingleInstance", lambda: SimpleNamespace(
        acquire=lambda: True, release=lambda: released.append("released"),
    ))
    monkeypatch.setattr(app, "_install_native_stderr_filter", lambda: None)
    monkeypatch.setattr(app, "_migrate_windows_file_icon", lambda: None)
    monkeypatch.setattr(
        app, "Api", lambda *a, **kw: (_ for _ in ()).throw(DatabaseVersionError(99, 1))
    )

    def no_window(*args, **kwargs):
        raise RuntimeError("native window unavailable")

    monkeypatch.setattr(startup, "show_startup_error", no_window)
    with pytest.raises(SystemExit) as exit_info:
        app.main()
    assert exit_info.value.code == 1
    assert released == ["released"]


def test_recovery_page_and_downloads_are_offline_until_clicked(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.webbrowser, "open", lambda url: calls.append(url))
    html = startup.startup_error_html(DatabaseVersionError(99, SCHEMA_VERSION))
    assert "现有数据已保留" in html
    assert "查看测试版下载" in html
    assert "停止自动退回旧版" in startup.startup_error_html(update_recovery=True)
    assert calls == []
    startup.StartupActions().open_downloads("beta")
    startup.StartupActions().open_downloads("https://example.invalid")
    assert calls == [
        "https://github.com/BITFSAE/tidoc/releases",
        "https://tidoc.bitfsae.com/start/install.html",
    ]
