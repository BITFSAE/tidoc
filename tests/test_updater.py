import hashlib
import json
import ssl
import urllib.error
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest

from tidoc.services.updater import (
    CoreUpdateManager,
    WINDOWS_INSTALLER_SCRIPT,
    _prune_old_component_versions,
    check_updates,
    download_update,
    downloaded_core_update_info,
    extract_core_update_archive,
    install_print_component,
    installed_component_info,
    installed_component_version,
    load_manifest,
    open_downloaded_core_update,
    parse_version,
    sha256_file,
    version_gt,
)


def test_version_compare():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert version_gt("0.1.1", "0.1.0")
    assert version_gt("0.2.0", "0.1.9")
    assert not version_gt("0.1.0", "0.1.0")


def test_extract_silent_windows_update_requires_one_safe_root(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("tidoc/tidoc.exe", b"new-core")
        zf.writestr("tidoc/_internal/runtime.bin", b"runtime")

    staged = extract_core_update_archive(
        archive, tmp_path / "stage", root_name="tidoc", plat="windows"
    )

    assert (staged / "tidoc.exe").read_bytes() == b"new-core"
    assert "--update-health-file" in WINDOWS_INSTALLER_SCRIPT
    assert "Move-Item -LiteralPath $StagedDir -Destination $AppDir" in WINDOWS_INSTALLER_SCRIPT


def test_extract_silent_update_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("tidoc/tidoc.exe", b"new-core")
        zf.writestr("tidoc/../outside.txt", b"bad")

    with pytest.raises(ValueError, match="不安全路径"):
        extract_core_update_archive(
            archive, tmp_path / "stage", root_name="tidoc", plat="windows"
        )


def test_extract_macos_update_accepts_ditto_metadata(monkeypatch, tmp_path):
    from tidoc.services import updater

    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("tidoc.app/Contents/MacOS/tidoc", b"new-core")
        zf.writestr("__MACOSX/tidoc.app/Contents/MacOS/._tidoc", b"metadata")
    real_exists = Path.exists
    monkeypatch.setattr(
        updater.Path,
        "exists",
        lambda candidate: False
        if candidate == Path("/usr/bin/ditto")
        else real_exists(candidate),
    )

    staged = extract_core_update_archive(
        archive, tmp_path / "stage", root_name="tidoc.app", plat="macos"
    )

    assert (staged / "Contents" / "MacOS" / "tidoc").read_bytes() == b"new-core"


def test_core_update_manager_downloads_verifies_and_stages(monkeypatch, tmp_path):
    from tidoc.services import updater

    archive = tmp_path / "tidoc-core-windows-v9.9.9-update.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("tidoc/tidoc.exe", b"new-core")
    asset = {
        "version": "9.9.9",
        "auto_update": {
            "url": archive.as_uri(),
            "filename": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "size": archive.stat().st_size,
            "root_name": "tidoc",
        },
    }
    monkeypatch.setattr(updater, "current_platform", lambda: "windows")
    monkeypatch.setattr(updater, "silent_core_update_supported", lambda _asset=None: True)
    manager = CoreUpdateManager(tmp_path / "updates")

    manager._download_worker(asset["auto_update"], asset["version"])
    status = manager.status()

    assert status["state"] == "ready"
    assert status["progress"] == 1.0
    assert (Path(status["stage_dir"]) / "tidoc.exe").read_bytes() == b"new-core"
    assert downloaded_core_update_info(tmp_path / "updates", "windows")["version"] == "9.9.9"


def test_check_updates_from_file_manifest(tmp_path):
    artifact = tmp_path / "tidoc-print-windows-v0.2.0.exe"
    artifact.write_bytes(b"print")
    manifest = {
        "components": {
            "core": {
                "name": "tidoc 核心",
                "latest": "0.1.0",
                "platforms": {
                    "windows": {
                        "url": "https://example.com/core.exe",
                        "sha256": "0" * 64,
                        "filename": "core.exe",
                    }
                },
            },
            "print": {
                "name": "打印导出组件",
                "latest": "0.2.0",
                "platforms": {
                    "windows": {
                        "url": artifact.as_uri(),
                        "sha256": sha256_file(artifact),
                        "filename": artifact.name,
                    }
                },
            },
        }
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")

    status = check_updates(tmp_path / "components", manifest_path.as_uri(), plat="windows")
    updates = {item["component"]: item for item in status["updates"]}
    assert updates["core"]["available"] is False
    assert updates["print"]["available"] is True


def test_missing_component_executable_is_reported_as_repairable(tmp_path):
    components = tmp_path / "components"
    marker = components / "print" / "windows" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "component": "print",
        "version": "0.2.0",
        "platform": "windows",
        "executable": str(marker.parent / "0.2.0" / "tidoc_print.exe"),
        "installed_sha256": "0" * 64,
    }), "utf-8")

    info = installed_component_info(components, "print", "windows")

    assert info["valid"] is False
    assert info["needs_repair"] is True
    assert info["issue"] == "missing_executable"
    assert installed_component_version(components, "print", "windows") == ""


def test_check_updates_offers_repair_when_recorded_component_is_missing(tmp_path):
    components = tmp_path / "components"
    marker = components / "print" / "windows" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "component": "print",
        "version": "0.2.0",
        "platform": "windows",
        "executable": str(marker.parent / "0.2.0" / "tidoc_print.exe"),
    }), "utf-8")
    manifest = {
        "components": {
            "print": {
                "name": "打印导出组件",
                "latest": "0.2.0",
                "platforms": {
                    "windows": {
                        "url": "https://example.com/tidoc_print.exe",
                        "sha256": "0" * 64,
                    }
                },
            }
        }
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")

    status = check_updates(components, manifest_path.as_uri(), plat="windows")
    item = status["updates"][0]

    assert item["current_version"] == "0.2.0"
    assert item["available"] is True
    assert item["needs_repair"] is True
    assert item["state"] == "available"


def test_component_checksum_mismatch_requires_repair(tmp_path):
    executable = tmp_path / "components" / "print" / "windows" / "0.2.0" / "tidoc_print.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"damaged")
    marker = executable.parents[1] / "current.json"
    marker.write_text(json.dumps({
        "component": "print",
        "version": "0.2.0",
        "platform": "windows",
        "executable": str(executable),
        "installed_sha256": "0" * 64,
    }), "utf-8")

    info = installed_component_info(tmp_path / "components", "print", "windows")

    assert info["valid"] is False
    assert info["needs_repair"] is True
    assert info["issue"] == "checksum_mismatch"


def test_print_install_records_and_validates_installed_executable(tmp_path):
    artifact = tmp_path / "tidoc_print.exe"
    artifact.write_bytes(b"print-component")
    manifest = {
        "components": {
            "print": {
                "name": "打印导出组件",
                "latest": "0.2.0",
                "platforms": {
                    "windows": {
                        "url": artifact.as_uri(),
                        "sha256": sha256_file(artifact),
                        "filename": artifact.name,
                        "format": "exe",
                        "executable_name": artifact.name,
                    }
                },
            }
        }
    }
    components = tmp_path / "components"

    result = install_print_component(
        manifest,
        components,
        tmp_path / "updates",
        plat="windows",
    )
    info = installed_component_info(components, "print", "windows")

    assert result.installed_path is not None
    assert info["valid"] is True
    assert info["version"] == "0.2.0"
    assert installed_component_version(components, "print", "windows") == "0.2.0"


def test_print_install_prunes_previous_version_directories(tmp_path):
    artifact = tmp_path / "tidoc_print.exe"

    def manifest_for(version: str) -> dict:
        artifact.write_bytes(f"print-component-{version}".encode("utf-8"))
        return {
            "components": {
                "print": {
                    "name": "打印导出组件",
                    "latest": version,
                    "platforms": {
                        "windows": {
                            "url": artifact.as_uri(),
                            "sha256": sha256_file(artifact),
                            "filename": artifact.name,
                            "format": "exe",
                            "executable_name": artifact.name,
                        }
                    },
                }
            }
        }

    components = tmp_path / "components"
    install_print_component(
        manifest_for("0.1.9"), components, tmp_path / "updates", plat="windows"
    )
    legacy = components / "print" / "windows" / "0.1.8"
    legacy.mkdir(parents=True)
    (legacy / "tidoc_print.exe").write_bytes(b"stale")

    install_print_component(
        manifest_for("0.2.0"), components, tmp_path / "updates", plat="windows"
    )

    platform_root = components / "print" / "windows"
    assert [p.name for p in platform_root.iterdir() if p.is_dir()] == ["0.2.0"]
    assert (platform_root / "current.json").exists()
    assert installed_component_version(components, "print", "windows") == "0.2.0"


def test_print_install_keeps_unknown_dir_when_version_missing(tmp_path):
    artifact = tmp_path / "tidoc_print.exe"
    artifact.write_bytes(b"print-component")
    manifest = {
        "components": {
            "print": {
                "name": "打印导出组件",
                "platforms": {
                    "windows": {
                        "url": artifact.as_uri(),
                        "sha256": sha256_file(artifact),
                        "filename": artifact.name,
                        "format": "exe",
                        "executable_name": artifact.name,
                    }
                },
            }
        }
    }
    components = tmp_path / "components"
    leftover = components / "print" / "windows" / "0.1.8"
    leftover.mkdir(parents=True)
    (leftover / "tidoc_print.exe").write_bytes(b"stale")

    install_print_component(
        manifest, components, tmp_path / "updates", plat="windows"
    )

    platform_root = components / "print" / "windows"
    assert sorted(p.name for p in platform_root.iterdir() if p.is_dir()) == ["unknown"]
    assert (platform_root / "unknown" / artifact.name).exists()


def test_prune_skips_when_keep_version_empty(tmp_path):
    kept = tmp_path / "print" / "windows" / "unknown"
    kept.mkdir(parents=True)
    (kept / "tidoc_print.exe").write_bytes(b"keep")

    _prune_old_component_versions(tmp_path, "print", "windows", "")

    assert kept.exists()


def test_frozen_core_does_not_treat_bundled_package_fragment_as_component(monkeypatch, tmp_path):
    from tidoc.services import printing

    monkeypatch.setattr(printing.sys, "frozen", True, raising=False)

    status = printing.component_status(tmp_path / "components")

    assert status["available"] is False
    assert status["mode"] == "missing"


def test_source_run_prefers_workspace_print_code_over_installed_component(monkeypatch, tmp_path):
    import tidoc_print
    from tidoc.services import printing

    monkeypatch.delattr(printing.sys, "frozen", raising=False)
    monkeypatch.setattr(tidoc_print, "is_available", lambda: True)
    monkeypatch.setattr(
        printing,
        "installed_component_info",
        lambda *_args, **_kwargs: {
            "valid": True,
            "version": "0.1.19",
            "executable": str(tmp_path / "old-tidoc-print"),
        },
    )

    status = printing.component_status(tmp_path / "components")

    assert status["available"] is True
    assert status["mode"] == "python"
    assert status["version"] == "0.1.20"


def test_frozen_core_reports_removed_component_as_needing_repair(monkeypatch, tmp_path):
    from tidoc.services import printing

    components = tmp_path / "components"
    marker = components / "print" / "windows" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "component": "print",
        "version": "0.2.0",
        "platform": "windows",
        "executable": str(marker.parent / "0.2.0" / "tidoc_print.exe"),
    }), "utf-8")
    monkeypatch.setattr(printing.sys, "frozen", True, raising=False)
    monkeypatch.setattr(printing.sys, "platform", "win32")

    status = printing.component_status(components)

    assert status["available"] is False
    assert status["mode"] == "repair"
    assert status["needs_repair"] is True


def test_core_download_records_pending_install_state(tmp_path):
    artifact = tmp_path / "tidoc-core-windows-v0.2.0.exe"
    artifact.write_bytes(b"core")
    manifest = {
        "components": {
            "core": {
                "name": "tidoc 核心",
                "latest": "0.2.0",
                "platforms": {
                    "windows": {
                        "url": artifact.as_uri(),
                        "sha256": sha256_file(artifact),
                        "filename": artifact.name,
                    }
                },
            }
        }
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")

    result = download_update(manifest, "core", tmp_path / "updates", plat="windows")
    assert result.file_path.exists()

    downloaded = downloaded_core_update_info(tmp_path / "updates", plat="windows")
    assert downloaded["version"] == "0.2.0"
    assert downloaded["file_path"] == str(result.file_path)

    status = check_updates(
        tmp_path / "components",
        manifest_path.as_uri(),
        plat="windows",
        updates_dir=tmp_path / "updates",
    )
    core = status["updates"][0]
    assert core["available"] is True
    assert core["downloaded"] is True
    assert core["state"] == "downloaded"
    assert core["downloaded_path"] == str(result.file_path)


def test_open_downloaded_core_update_uses_platform_opener(monkeypatch, tmp_path):
    from tidoc.services import updater

    opened = []
    package = tmp_path / "tidoc-core-windows-v0.2.0.exe"
    package.write_bytes(b"core")
    marker = tmp_path / "updates" / "core" / "windows" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "component": "core",
        "version": "0.2.0",
        "platform": "windows",
        "file_path": str(package),
        "sha256": sha256_file(package),
    }), "utf-8")

    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.os, "startfile", lambda path: opened.append(str(path)), raising=False)

    info = open_downloaded_core_update(tmp_path / "updates", plat="windows")
    assert info["version"] == "0.2.0"
    assert opened == [str(package)]


def test_load_manifest_falls_back_to_macos_system_trust(monkeypatch):
    from tidoc.services import updater

    def fail_urlopen(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    def fake_run(cmd, capture_output, timeout, check):
        assert cmd[0].endswith("curl")
        assert "--fail" in cmd
        return SimpleNamespace(returncode=0, stdout=b'{"components": {}}', stderr=b"")

    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater.urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    assert load_manifest("https://img.bitfsae.com/tidoc/manifest.json") == {"components": {}}


def test_load_manifest_reports_certificate_fallback_failure(monkeypatch):
    from tidoc.services import updater

    def fail_urlopen(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    def fake_run(cmd, capture_output, timeout, check):
        return SimpleNamespace(returncode=60, stdout=b"", stderr=b"certificate failed")

    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater.urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="系统信任链"):
        load_manifest("https://img.bitfsae.com/tidoc/manifest.json")
