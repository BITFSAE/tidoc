import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_build_manifest_script(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.3.0.exe").write_bytes(b"core-win")
    (release / "tidoc-core-windows-v0.3.0-update.zip").write_bytes(b"core-win-update")
    (release / "tidoc-core-macos-v0.3.0.dmg").write_bytes(b"core-mac")
    (release / "tidoc-core-macos-v0.3.0-update.zip").write_bytes(b"core-mac-update")
    (release / "tidoc-print-windows-v0.3.0.exe").write_bytes(b"print-win")

    script = Path(__file__).resolve().parents[1] / "scripts" / "build_manifest.py"
    subprocess.run(
        [sys.executable, str(script), "--release-dir", str(release), "--version", "0.3.0"],
        check=True,
    )

    manifest = json.loads((release / "manifest.json").read_text("utf-8"))
    assert manifest["components"]["core"]["latest"] == "0.3.0"
    assert "windows" in manifest["components"]["core"]["platforms"]
    assert "macos" in manifest["components"]["core"]["platforms"]
    assert manifest["components"]["core"]["platforms"]["windows"]["auto_update"]["root_name"] == "tidoc"
    assert manifest["components"]["core"]["platforms"]["macos"]["auto_update"]["root_name"] == "tidoc.app"
    assert manifest["components"]["print"]["platforms"]["windows"]["key"].startswith("tidoc/print/windows/")
    assert (release / "upload_plan.tsv").read_text("utf-8").count("\n") == 5


def test_build_manifest_keeps_print_version_independent_from_core(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.4.0.exe").write_bytes(b"core-win")
    (release / "tidoc-print-windows-v0.2.1.exe").write_bytes(b"print-win")
    (release / "tidoc-print-macos-v0.2.1.zip").write_bytes(b"print-mac")

    script = Path(__file__).resolve().parents[1] / "scripts" / "build_manifest.py"
    subprocess.run(
        [sys.executable, str(script), "--release-dir", str(release), "--version", "0.4.0"],
        check=True,
    )

    manifest = json.loads((release / "manifest.json").read_text("utf-8"))
    assert manifest["components"]["core"]["latest"] == "0.4.0"
    assert manifest["components"]["print"]["latest"] == "0.2.1"


def test_build_manifest_uses_structured_release_notes(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.4.0.exe").write_bytes(b"core")
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps(["更新界面", "减少启动检查频率"], ensure_ascii=False), "utf-8")

    script = Path(__file__).resolve().parents[1] / "scripts" / "build_manifest.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--release-dir",
            str(release),
            "--version",
            "0.4.0",
            "--notes-file",
            str(notes),
        ],
        check=True,
    )

    manifest = json.loads((release / "manifest.json").read_text("utf-8"))
    assert manifest["components"]["core"]["notes"] == ["更新界面", "减少启动检查频率"]


def test_set_version_updates_frontend_asset_cache_keys(tmp_path):
    (tmp_path / "tidoc" / "web").mkdir(parents=True)
    (tmp_path / "tidoc_print").mkdir()
    (tmp_path / "tidoc" / "__init__.py").write_text('__version__ = "0.1.0"\n', "utf-8")
    (tmp_path / "tidoc_print" / "__init__.py").write_text('__version__ = "0.1.0"\n', "utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2026-09-13 · v9.8.7\n\n### Fixed\n\n- 修复更新说明。\n",
        "utf-8",
    )
    index = tmp_path / "tidoc" / "web" / "index.html"
    index.write_text(
        '<link href="styles.css?v=0.1.0"><script src="api.js?v=0.1.0"></script>'
        '<script src="schema-form.js?v=0.1.0"></script>'
        '<script src="adapter-ui.js?v=0.1.0"></script>'
        '<script src="app.js?v=0.1.0"></script>',
        "utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "set_version.py"

    subprocess.run([sys.executable, str(script), "9.8.7"], cwd=tmp_path, check=True)

    assert '__version__ = "9.8.7"' in (tmp_path / "tidoc" / "__init__.py").read_text("utf-8")
    assert '__version__ = "0.1.0"' in (tmp_path / "tidoc_print" / "__init__.py").read_text("utf-8")
    updated = index.read_text("utf-8")
    assert "styles.css?v=9.8.7" in updated
    assert "api.js?v=9.8.7" in updated
    assert "schema-form.js?v=9.8.7" in updated
    assert "adapter-ui.js?v=9.8.7" in updated
    assert "app.js?v=9.8.7" in updated
    release_info = (tmp_path / "tidoc" / "release_info.py").read_text("utf-8")
    assert "RELEASE_VERSION = '9.8.7'" in release_info
    assert '"修复更新说明。"' in release_info


def _run_manifest(release, *args, check=True):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_manifest.py"
    return subprocess.run(
        [sys.executable, str(script), "--release-dir", str(release), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


def _stable_manifest(tmp_path, **latest):
    path = tmp_path / "stable.json"
    path.write_text(json.dumps({"components": {name: {"latest": version} for name, version in latest.items()}}), "utf-8")
    return str(path)


def test_prerelease_goes_to_the_beta_manifest_and_leaves_the_stable_one_alone(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.5.0-beta.1.exe").write_bytes(b"core-win")
    (release / "tidoc-core-windows-v0.5.0-beta.1-update.zip").write_bytes(b"core-win-update")
    (release / "tidoc-core-macos-v0.5.0-beta.1.dmg").write_bytes(b"core-mac")
    (release / "tidoc-core-macos-v0.5.0-beta.1-update.zip").write_bytes(b"core-mac-update")
    # 0.2.1 is newer than anything the stable channel serves, so it ships with the beta.
    (release / "tidoc-print-windows-v0.2.1.exe").write_bytes(b"print-win")

    _run_manifest(release, "--version", "0.5.0-beta.1", "--stable-manifest", _stable_manifest(tmp_path, core="0.4.0", print="0.2.0"))

    assert not (release / "manifest.json").exists()      # 稳定版清单只由正式版发布改写
    manifest = json.loads((release / "manifest-beta.json").read_text("utf-8"))
    core = manifest["components"]["core"]
    assert manifest["channel"] == "beta" and core["latest"] == "0.5.0-beta.1"
    windows = core["platforms"]["windows"]
    assert windows["filename"] == "tidoc-core-windows-v0.5.0-beta.1.exe"
    assert windows["auto_update"]["filename"] == "tidoc-core-windows-v0.5.0-beta.1-update.zip"
    assert core["platforms"]["macos"]["auto_update"]["filename"] == "tidoc-core-macos-v0.5.0-beta.1-update.zip"
    assert manifest["components"]["print"]["latest"] == "0.2.1"


def test_prerelease_never_republishes_a_component_version_the_stable_channel_serves(tmp_path):
    # Components are rebuilt on every tag and PyInstaller output is not reproducible. Uploading the
    # same file name again would change the bytes behind the stable manifest's sha256.
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.5.0-beta.1.exe").write_bytes(b"core-win")
    (release / "tidoc-print-windows-v0.2.0.exe").write_bytes(b"print-win")
    (release / "tidoc-ocr-macos-v0.3.0.zip").write_bytes(b"ocr-mac")

    _run_manifest(release, "--version", "0.5.0-beta.1", "--stable-manifest", _stable_manifest(tmp_path, core="0.4.0", print="0.2.0", ocr="0.3.0"))

    manifest = json.loads((release / "manifest-beta.json").read_text("utf-8"))
    assert set(manifest["components"]) == {"core"}
    plan = (release / "upload_plan.tsv").read_text("utf-8")
    assert "tidoc-core-windows-v0.5.0-beta.1.exe" in plan
    assert "tidoc-print" not in plan and "tidoc-ocr" not in plan


def test_prerelease_refuses_to_run_without_the_stable_manifest(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.5.0-beta.1.exe").write_bytes(b"core-win")

    missing = _run_manifest(release, "--version", "0.5.0-beta.1", check=False)
    assert missing.returncode != 0 and "--stable-manifest" in missing.stderr
    unreadable = _run_manifest(release, "--version", "0.5.0-beta.1", "--stable-manifest", str(tmp_path / "nope.json"), check=False)
    assert unreadable.returncode != 0 and "Cannot read the stable manifest" in unreadable.stderr
    assert not (release / "manifest-beta.json").exists()


def test_stable_release_still_writes_the_stable_manifest_with_every_component(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.5.0.exe").write_bytes(b"core-win")
    (release / "tidoc-print-windows-v0.2.0.exe").write_bytes(b"print-win")
    _run_manifest(release, "--version", "0.5.0")
    manifest = json.loads((release / "manifest.json").read_text("utf-8"))
    assert manifest["channel"] == "stable" and set(manifest["components"]) == {"core", "print"}
    assert not (release / "manifest-beta.json").exists()


def test_release_versions_must_be_dotted_alphanumeric():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from release_version import validate_version

    for good in ("0.1.39", "0.1.39-beta.1", "1.0.0-rc.2", "1.0.0-alpha"):
        assert validate_version(good) == good
    for bad in ("0.1", "v0.1.39", "0.1.39-", "0.1.39-rc-1", "0.1.39-beta..1", "0.1.39+build", "1.2.3.4"):
        with pytest.raises(ValueError):
            validate_version(bad)


def test_workflow_rejects_a_malformed_tag_before_building_anything():
    script = Path(__file__).resolve().parents[1] / "scripts" / "release_version.py"
    assert subprocess.run([sys.executable, str(script), "v0.1.39-beta.1"]).returncode == 0
    assert subprocess.run([sys.executable, str(script), "0.1.39-rc-1"], capture_output=True).returncode == 1
