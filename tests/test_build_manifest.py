import json
import subprocess
import sys
from pathlib import Path


def test_build_manifest_script(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "tidoc-core-windows-v0.3.0.exe").write_bytes(b"core-win")
    (release / "tidoc-core-macos-v0.3.0.dmg").write_bytes(b"core-mac")
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
    assert manifest["components"]["print"]["platforms"]["windows"]["key"].startswith("tidoc/print/windows/")
    assert (release / "upload_plan.tsv").read_text("utf-8").count("\n") == 3


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
    index = tmp_path / "tidoc" / "web" / "index.html"
    index.write_text(
        '<link href="styles.css?v=0.1.0"><script src="api.js?v=0.1.0"></script>'
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
    assert "app.js?v=9.8.7" in updated
