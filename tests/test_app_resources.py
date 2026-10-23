import sys
from pathlib import Path
from types import SimpleNamespace

from tidoc import __version__
from tidoc import app


def test_web_dir_prefers_pyinstaller_meipass(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle" / "tidoc" / "web"
    bundled.mkdir(parents=True)
    (bundled / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(app.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert app.web_dir() == bundled


def test_web_dir_uses_source_tree_without_bundle(monkeypatch):
    monkeypatch.delattr(app.sys, "_MEIPASS", raising=False)

    assert (app.web_dir() / "index.html").is_file()


def test_web_app_url_and_assets_are_versioned(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    from html.parser import HTMLParser
    from urllib.parse import urlsplit, parse_qs

    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])
            if tag == "script" and "src" in attrs:
                self.urls.append(attrs["src"])
            if tag == "link" and attrs.get("rel") == "stylesheet":
                self.urls.append(attrs["href"])

    assets = Assets()
    assets.feed((app.web_dir() / "index.html").read_text("utf-8"))
    assert app.web_app_url(index) == index.as_uri()
    assert len(assets.ids) == len(set(assets.ids)), "Duplicate DOM IDs break control lookup"
    paths = []
    for url in assets.urls:
        parsed = urlsplit(url)
        if parsed.scheme:
            continue
        paths.append(parsed.path)
        assert (app.web_dir() / parsed.path).is_file(), url
        assert parse_qs(parsed.query).get("v") == [__version__], url
    for dependency in ("api.js", "select.js", "schema-form.js", "adapter-ui.js", "qr-ticket.js", "bindle-ui.js"):
        assert paths.index(dependency) < paths.index("app.js")


def test_update_health_path_requires_absolute_path(tmp_path):
    absolute = tmp_path / "health.json"
    assert app._update_health_path_from_argv(
        ["--update-health-file", str(absolute)]
    ) == absolute.resolve()
    assert app._update_health_path_from_argv(
        ["--update-health-file", "relative.json"]
    ) is None


def test_windows_file_icon_registration_migrates_from_removed_loose_icon(
    monkeypatch, tmp_path
):
    executable = tmp_path / "Tidoc" / "tidoc.exe"
    legacy_icon = executable.with_name("tidoc-file.ico")
    writes = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_QUERY_VALUE=1,
        KEY_SET_VALUE=2,
        REG_SZ=1,
        OpenKey=lambda *_args: Key(),
        QueryValueEx=lambda *_args: (f'"{legacy_icon}",0', 1),
        SetValueEx=lambda *args: writes.append(args),
    )
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "executable", str(executable))
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert app._migrate_windows_file_icon() is True
    assert writes[0][-1] == f"{executable.resolve()},0"


def test_webview_settings_allow_tax_site_certificate_fallback():
    import webview

    original_downloads = webview.settings["ALLOW_DOWNLOADS"]
    original_ssl = webview.settings["IGNORE_SSL_ERRORS"]
    try:
        app._configure_webview_settings()
        assert webview.settings["ALLOW_DOWNLOADS"] is True
        assert webview.settings["IGNORE_SSL_ERRORS"] is True
    finally:
        webview.settings["ALLOW_DOWNLOADS"] = original_downloads
        webview.settings["IGNORE_SSL_ERRORS"] = original_ssl


def test_native_window_background_matches_theme_preference(monkeypatch):
    class StubApi:
        def __init__(self, mode):
            self.mode = mode

        def _preference_value(self, key, default):
            assert key == "tidoc.themeMode"
            return self.mode or default

    assert app._initial_window_background(StubApi("dark")) == "#141414"
    assert app._initial_window_background(StubApi("light")) == "#f4f1ea"
    monkeypatch.setattr(app, "_system_uses_dark_mode", lambda: True)
    assert app._initial_window_background(StubApi("system")) == "#141414"


def test_normal_startup_filters_known_native_and_pdf_noise():
    patterns = "\n".join(app._STDERR_SUPPRESS_PATTERNS)
    assert "IMKCFRunLoopWakeUpReliable" in patterns
    assert "Ignoring wrong pointing object" in patterns
