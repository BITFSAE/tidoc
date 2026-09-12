"""联网更新服务（腾讯云 COS manifest）。

客户端只读取公开 manifest 和更新包；腾讯云密钥只存在于 GitHub Actions。
核心程序第一版只负责下载并校验安装包，可选组件支持下载安装到数据目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from tidoc import __version__ as CORE_VERSION

MANIFEST_URL = "https://img.bitfsae.com/tidoc/manifest.json"
USER_AGENT = f"tidoc/{CORE_VERSION}"
COMPONENT_PRINT = "print"
COMPONENT_OCR = "ocr"
COMPONENT_CORE = "core"
# 更新对话框逐行展示的可选组件（核心不在其中，单独渲染）
INSTALLABLE_COMPONENTS = (COMPONENT_PRINT, COMPONENT_OCR)
CORE_APP_NAMES = {"windows": "tidoc.exe", "macos": "tidoc.app"}


WINDOWS_INSTALLER_SCRIPT = r'''
param(
    [Parameter(Mandatory=$true)][string]$AppDir,
    [Parameter(Mandatory=$true)][string]$StagedDir,
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][string]$ExpectedVersion,
    [Parameter(Mandatory=$true)][string]$HealthFile,
    [int]$OldPid = 0
)
$ErrorActionPreference = "Stop"
function Start-Tidoc([string]$Directory, [bool]$WithHealth) {
    $target = Join-Path $Directory "tidoc.exe"
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "tidoc.exe missing" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $target
    $psi.WorkingDirectory = $Directory
    $psi.UseShellExecute = $true
    if ($WithHealth) { $psi.Arguments = "--update-health-file `"$HealthFile`"" }
    return [System.Diagnostics.Process]::Start($psi)
}
function Move-OldTidoc([string]$Source, [string]$DestinationLeaf) {
    $attempt = 0
    while ($attempt -lt 30) {
        try {
            Rename-Item -LiteralPath $Source -NewName $DestinationLeaf -ErrorAction Stop
            return
        } catch {
            $attempt += 1
            if ($attempt -ge 30) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}
if ($OldPid -gt 0) {
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        Stop-Process -Id $OldPid -Force -ErrorAction SilentlyContinue
        try { Wait-Process -Id $OldPid -Timeout 10 -ErrorAction SilentlyContinue } catch {}
    }
}
$backup = "$AppDir.old-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
$movedOld = $false
$new = $null
try {
    Move-OldTidoc $AppDir (Split-Path $backup -Leaf)
    $movedOld = $true
    Move-Item -LiteralPath $StagedDir -Destination $AppDir
    foreach ($unins in @("unins000.exe", "unins000.dat")) {
        $src = Join-Path $backup $unins
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $AppDir $unins) -Force
        }
    }
    Remove-Item -LiteralPath $HealthFile -Force -ErrorAction SilentlyContinue
    $new = Start-Tidoc $AppDir $true
    $deadline = (Get-Date).AddSeconds(45)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        $new.Refresh()
        if ($new.HasExited) { break }
        if (Test-Path -LiteralPath $HealthFile -PathType Leaf) {
            try {
                $health = Get-Content -LiteralPath $HealthFile -Raw | ConvertFrom-Json
                if ([int]$health.pid -eq $new.Id -and [string]$health.version -eq $ExpectedVersion) {
                    $healthy = $true
                    break
                }
            } catch { break }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $healthy) { throw "new Tidoc did not report healthy" }
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
} catch {
    if ($new -and -not $new.HasExited) { Stop-Process -Id $new.Id -Force -ErrorAction SilentlyContinue }
    if ($movedOld -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $backup -Destination $AppDir
        try { Start-Tidoc $AppDir $false | Out-Null } catch {}
    } elseif (Test-Path -LiteralPath $AppDir) {
        try { Start-Tidoc $AppDir $false | Out-Null } catch {}
    }
    exit 23
}
'''


MACOS_INSTALLER_SCRIPT = r'''#!/bin/sh
set -eu
APP_DIR="$1"
STAGED_DIR="$2"
WORK_DIR="$3"
EXPECTED_VERSION="$4"
HEALTH_FILE="$5"
OLD_PID="$6"
BACKUP_DIR="${APP_DIR}.old-$(date -u +%Y%m%d%H%M%S)"
i=0
while kill -0 "$OLD_PID" 2>/dev/null && [ "$i" -lt 80 ]; do sleep 0.25; i=$((i + 1)); done
if kill -0 "$OLD_PID" 2>/dev/null; then kill -9 "$OLD_PID" 2>/dev/null || true; fi
mv "$APP_DIR" "$BACKUP_DIR"
rollback() {
  if [ -n "${NEW_PID:-}" ]; then kill -9 "$NEW_PID" 2>/dev/null || true; fi
  rm -rf "$APP_DIR"
  mv "$BACKUP_DIR" "$APP_DIR"
  open -n "$APP_DIR" || true
}
trap rollback EXIT HUP INT TERM
mv "$STAGED_DIR" "$APP_DIR"
rm -f "$HEALTH_FILE"
"$APP_DIR/Contents/MacOS/tidoc" --update-health-file "$HEALTH_FILE" >/dev/null 2>&1 &
NEW_PID=$!
i=0
while [ "$i" -lt 180 ]; do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then exit 23; fi
  if [ -f "$HEALTH_FILE" ] && grep -Fq "\"version\": \"$EXPECTED_VERSION\"" "$HEALTH_FILE"; then
    trap - EXIT HUP INT TERM
    rm -rf "$BACKUP_DIR" "$WORK_DIR"
    exit 0
  fi
  sleep 0.25
  i=$((i + 1))
done
exit 23
'''


@dataclass(frozen=True)
class DownloadResult:
    component: str
    version: str
    platform: str
    file_path: Path
    sha256: str
    size: int
    installed_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "version": self.version,
            "platform": self.platform,
            "file_path": str(self.file_path),
            "sha256": self.sha256,
            "size": self.size,
            "installed_path": str(self.installed_path) if self.installed_path else "",
        }


def current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def parse_version(version: str) -> tuple[int, ...]:
    """解析简单 semver；非数字后缀会被忽略，够发布链路使用。"""
    cleaned = version.strip().lstrip("v")
    nums = []
    for part in cleaned.split("."):
        digits = []
        for ch in part:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        nums.append(int("".join(digits) or "0"))
    return tuple(nums or [0])


def version_gt(left: str, right: str) -> bool:
    a = parse_version(left)
    b = parse_version(right)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(url: str = MANIFEST_URL, timeout: int = 12) -> dict[str, Any]:
    try:
        raw = _read_url(url, timeout)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法读取更新清单：{exc}") from exc
    except RuntimeError as exc:
        raise RuntimeError(f"无法读取更新清单：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("更新清单不是有效 JSON。") from exc


def get_component(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    components = manifest.get("components") or {}
    comp = components.get(name)
    if not isinstance(comp, dict):
        raise KeyError(f"更新清单缺少组件：{name}")
    return comp


def get_platform_asset(manifest: dict[str, Any], component: str, plat: str | None = None) -> dict[str, Any]:
    plat = plat or current_platform()
    comp = get_component(manifest, component)
    platforms = comp.get("platforms") or {}
    asset = platforms.get(plat)
    if not isinstance(asset, dict):
        raise KeyError(f"{component} 没有 {plat} 更新包。")
    out = dict(asset)
    out.setdefault("component", component)
    out.setdefault("version", comp.get("latest") or "")
    out.setdefault("platform", plat)
    out.setdefault("name", comp.get("name") or component)
    out.setdefault("notes", comp.get("notes") or [])
    out.setdefault("force_update", bool(comp.get("force_update")))
    return out


def installed_component_version(components_dir: str | Path, component: str, plat: str | None = None) -> str:
    """Return the installed version only when its recorded executable is usable."""
    info = installed_component_info(components_dir, component, plat)
    return info["version"] if info["valid"] else ""


def installed_component_info(
    components_dir: str | Path,
    component: str,
    plat: str | None = None,
) -> dict[str, Any]:
    """Inspect a component marker and verify the installed executable.

    A marker on its own is not proof of an installation: users may remove the
    version directory, security software may quarantine the executable, or a
    previous install may have stopped halfway through.
    """
    marker = _component_root(components_dir, component, plat) / "current.json"
    if not marker.exists():
        return {
            "marker_exists": False,
            "valid": False,
            "needs_repair": False,
            "version": "",
            "executable": "",
            "issue": "not_installed",
        }
    try:
        data = json.loads(marker.read_text("utf-8"))
    except Exception:
        return {
            "marker_exists": True,
            "valid": False,
            "needs_repair": True,
            "version": "",
            "executable": "",
            "issue": "invalid_marker",
        }

    version = data.get("version") or ""
    executable_value = data.get("executable") or ""
    executable = Path(executable_value) if executable_value else None
    if not version or executable is None or not executable.is_file():
        return {
            "marker_exists": True,
            "valid": False,
            "needs_repair": True,
            "version": version,
            "executable": executable_value,
            "issue": "missing_executable",
        }

    expected_installed_hash = (data.get("installed_sha256") or "").lower()
    if expected_installed_hash:
        try:
            actual_installed_hash = sha256_file(executable).lower()
        except OSError:
            actual_installed_hash = ""
        if actual_installed_hash != expected_installed_hash:
            return {
                "marker_exists": True,
                "valid": False,
                "needs_repair": True,
                "version": version,
                "executable": str(executable),
                "issue": "checksum_mismatch",
            }

    return {
        "marker_exists": True,
        "valid": True,
        "needs_repair": False,
        "version": version,
        "executable": str(executable),
        "issue": "",
    }


def downloaded_core_update_info(updates_dir: str | Path, plat: str | None = None) -> dict[str, Any]:
    marker = _core_update_marker(updates_dir, plat)
    if not marker.exists():
        return {}
    try:
        info = json.loads(marker.read_text("utf-8"))
    except Exception:
        return {}
    file_value = str(info.get("file_path") or "")
    file_path = Path(file_value) if file_value else None
    if file_path is None or not file_path.is_file():
        return {}
    info["file_path"] = str(file_path)
    return info


def check_updates(
    components_dir: str | Path,
    manifest_url: str = MANIFEST_URL,
    plat: str | None = None,
    updates_dir: str | Path | None = None,
) -> dict[str, Any]:
    plat = plat or current_platform()
    manifest = load_manifest(manifest_url)
    result: dict[str, Any] = {
        "manifest_url": manifest_url,
        "platform": plat,
        "current_core_version": CORE_VERSION,
        "updates": [],
        "components": manifest.get("components") or {},
    }
    for name in (COMPONENT_CORE, *INSTALLABLE_COMPONENTS):
        manifest_missing = False
        try:
            asset = get_platform_asset(manifest, name, plat)
        except KeyError:
            if name == COMPONENT_CORE:
                continue
            if name != COMPONENT_OCR:
                continue
            # OCR 组件刚随本版本新增、线上 manifest 可能还未发布时，
            # 也在更新页显示一行，避免用户看不到入口。
            asset = {}
            manifest_missing = True
        if name == COMPONENT_CORE:
            current = CORE_VERSION
            component_info: dict[str, Any] = {}
        else:
            component_info = installed_component_info(components_dir, name, plat)
            current = component_info["version"] if component_info["valid"] else ""
        latest = asset.get("version") or ""
        available = bool(
            latest
            and (
                not current
                or version_gt(latest, current)
                or component_info.get("needs_repair", False)
            )
        )
        downloaded: dict[str, Any] = {}
        if name == COMPONENT_CORE and updates_dir is not None:
            candidate = downloaded_core_update_info(updates_dir, plat)
            if candidate.get("version") == latest:
                downloaded = candidate
        result["updates"].append({
            "component": name,
            "name": asset.get("name") or (
                "OCR 识别组件" if name == COMPONENT_OCR
                else "打印导出组件" if name == COMPONENT_PRINT else name
            ),
            "current_version": component_info.get("version", current),
            "latest_version": latest,
            "available": available,
            "installed_valid": component_info.get("valid", True),
            "needs_repair": component_info.get("needs_repair", False),
            "install_issue": component_info.get("issue", ""),
            "manifest_missing": manifest_missing,
            "downloaded": bool(downloaded),
            "downloaded_path": downloaded.get("file_path", ""),
            "state": "current" if not available else ("downloaded" if downloaded else "available"),
            "asset": asset,
        })
    return result


def download_asset(
    asset: dict[str, Any],
    updates_dir: str | Path,
    timeout: int = 60,
) -> Path:
    url = asset.get("url")
    expected = (asset.get("sha256") or "").lower()
    if not url or not expected:
        raise ValueError("更新包缺少 url 或 sha256。")
    updates_dir = Path(updates_dir)
    updates_dir.mkdir(parents=True, exist_ok=True)
    filename = asset.get("filename") or url.rsplit("/", 1)[-1] or "update.bin"
    final = updates_dir / filename
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".part", dir=updates_dir)
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        _download_url(url, tmp_path, timeout)
        actual = sha256_file(tmp_path)
        if actual.lower() != expected:
            raise RuntimeError(f"SHA256 校验失败：期望 {expected}，实际 {actual}")
        tmp_path.replace(final)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return final


def download_update(
    manifest: dict[str, Any],
    component: str,
    updates_dir: str | Path,
    plat: str | None = None,
) -> DownloadResult:
    asset = get_platform_asset(manifest, component, plat)
    path = download_asset(asset, updates_dir)
    if component == COMPONENT_CORE:
        _write_core_update_marker(updates_dir, asset, path)
    return DownloadResult(
        component=component,
        version=asset.get("version") or "",
        platform=asset.get("platform") or current_platform(),
        file_path=path,
        sha256=asset.get("sha256") or "",
        size=path.stat().st_size,
    )


def open_downloaded_core_update(updates_dir: str | Path, plat: str | None = None) -> dict[str, Any]:
    info = downloaded_core_update_info(updates_dir, plat)
    file_path = info.get("file_path")
    if not file_path:
        raise FileNotFoundError("没有已下载的核心更新包。")
    launch_core_update_package(file_path)
    return info


def launch_core_update_package(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"更新包不存在：{p}")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif sys.platform.startswith("win"):
        os.startfile(str(p))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(p)])


def silent_core_update_supported(asset: dict[str, Any] | None = None) -> bool:
    """Whether this build can replace itself without showing an installer."""
    if not getattr(sys, "frozen", False) or current_platform() not in {"windows", "macos"}:
        return False
    if asset is not None and not isinstance(asset.get("auto_update"), dict):
        return False
    if current_platform() == "macos":
        try:
            if not os.access(installed_app_dir().parent, os.W_OK):
                return False
        except RuntimeError:
            return False
    return True


def installed_app_dir() -> Path:
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix.lower() == ".app":
                return parent
        raise RuntimeError("无法定位 Tidoc 应用包。")
    return executable.parent


def _safe_archive_member(
    member: zipfile.ZipInfo,
    root_name: str,
    *,
    allow_macos_metadata: bool = False,
) -> None:
    raw = member.filename.replace("\\", "/")
    if raw.startswith("/") or "//" in raw:
        raise ValueError(f"更新包包含不安全路径：{member.filename}")
    parts = PurePosixPath(raw.rstrip("/")).parts
    if not parts or any(part in {"", ".."} for part in parts):
        raise ValueError(f"更新包包含不安全路径：{member.filename}")
    # ditto --sequesterRsrc stores macOS extended attributes under __MACOSX.
    # Accept only metadata tied to the one expected application root.
    expected_root = parts[0] == root_name
    expected_macos_metadata = (
        allow_macos_metadata
        and parts[0] == "__MACOSX"
        and (len(parts) == 1 or parts[1] in {root_name, f"._{root_name}"})
    )
    if not expected_root and not expected_macos_metadata:
        raise ValueError(f"更新包包含不安全路径：{member.filename}")


def extract_core_update_archive(
    archive_path: str | Path,
    destination: str | Path,
    *,
    root_name: str,
    plat: str | None = None,
) -> Path:
    """Validate and extract a one-root core update archive."""
    plat = plat or current_platform()
    archive_path = Path(archive_path)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            _safe_archive_member(
                member,
                root_name,
                allow_macos_metadata=plat == "macos",
            )
        if plat == "macos" and Path("/usr/bin/ditto").exists():
            proc = subprocess.run(
                ["/usr/bin/ditto", "-x", "-k", str(archive_path), str(destination)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip() or "无法解压更新包。")
        else:
            archive.extractall(destination)
    staged = destination / root_name
    required = staged / CORE_APP_NAMES.get(plat, "")
    if plat == "macos":
        required = staged / "Contents" / "MacOS" / "tidoc"
    if not required.is_file():
        raise ValueError(f"更新包缺少 {required.name}。")
    if plat != "windows":
        required.chmod(required.stat().st_mode | 0o755)
    return staged


def launch_silent_core_update(
    stage_dir: str | Path,
    work_dir: str | Path,
    expected_version: str,
    *,
    app_dir: str | Path | None = None,
    current_pid: int | None = None,
) -> Path:
    """Hand a verified staged app to a detached platform helper."""
    if not silent_core_update_supported():
        raise RuntimeError("当前运行方式不支持静默更新，请使用安装包更新。")
    app = Path(app_dir).resolve() if app_dir else installed_app_dir()
    stage = Path(stage_dir).resolve()
    work = Path(work_dir).resolve()
    if not app.is_absolute() or not stage.is_absolute() or not work.is_absolute():
        raise ValueError("更新路径必须是绝对路径。")
    expected_name = "tidoc.exe" if current_platform() == "windows" else "tidoc"
    expected_path = stage / expected_name
    if current_platform() == "macos":
        expected_path = stage / "Contents" / "MacOS" / expected_name
    if not expected_path.is_file():
        raise FileNotFoundError("已下载的更新目录不完整，请重新下载。")

    health_file = work / "update-health.json"
    if current_platform() == "windows":
        script = work / "install-helper.ps1"
        script.write_text(WINDOWS_INSTALLER_SCRIPT, "utf-8")
        powershell = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        command = [
            str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-AppDir", str(app), "-StagedDir", str(stage), "-WorkDir", str(work),
            "-ExpectedVersion", expected_version, "-HealthFile", str(health_file),
            "-OldPid", str(current_pid or os.getpid()),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, cwd=str(work.parent),
            creationflags=flags,
        )
    else:
        script = work / "install-helper.sh"
        script.write_text(MACOS_INSTALLER_SCRIPT, "utf-8")
        script.chmod(0o700)
        subprocess.Popen(
            [str(script), str(app), str(stage), str(work), expected_version,
             str(health_file), str(current_pid or os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, cwd=str(work.parent), start_new_session=True,
        )
    return script


class CoreUpdateManager:
    """Background download, verification and staging for a clean restart update."""

    def __init__(self, updates_dir: str | Path):
        self.updates_dir = Path(updates_dir)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle", "version": "", "progress": 0.0,
            "downloaded_bytes": 0, "total_bytes": 0, "speed_bps": 0.0,
            "stage": "", "stage_dir": "", "file_path": "", "error": "",
            "install_supported": silent_core_update_supported(),
        }
        self._restore_ready_state()

    def _restore_ready_state(self) -> None:
        info = downloaded_core_update_info(self.updates_dir)
        version = str(info.get("version") or "")
        stage_value = str(info.get("stage_dir") or "")
        stage = Path(stage_value) if stage_value else None
        if version and not version_gt(version, CORE_VERSION):
            try:
                _core_update_marker(self.updates_dir).unlink()
            except OSError:
                pass
            return
        if version and stage and stage.is_dir() and info.get("package_kind") == "silent":
            self._state.update({
                "state": "ready", "version": version, "progress": 1.0,
                "downloaded_bytes": int(info.get("size") or 0),
                "total_bytes": int(info.get("size") or 0),
                "stage": "ready", "stage_dir": str(stage),
                "file_path": str(info.get("file_path") or ""),
            })

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self, asset: dict[str, Any]) -> dict[str, Any]:
        auto_asset = asset.get("auto_update") or {}
        version = str(asset.get("version") or auto_asset.get("version") or "")
        if not isinstance(auto_asset, dict) or not auto_asset.get("url"):
            raise RuntimeError("此版本没有适用于本机的一键更新包。")
        if not silent_core_update_supported(asset):
            raise RuntimeError("当前运行方式不支持一键更新，请使用安装包更新。")
        if not version_gt(version, CORE_VERSION):
            raise RuntimeError("当前已是最新版本。")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            if self._state.get("state") == "ready" and self._state.get("version") == version:
                return self.status()
            self._state.update({
                "state": "downloading", "version": version, "progress": 0.0,
                "downloaded_bytes": 0, "total_bytes": int(auto_asset.get("size") or 0),
                "speed_bps": 0.0, "stage": "download", "stage_dir": "",
                "file_path": "", "error": "", "install_supported": True,
            })
            self._thread = threading.Thread(
                target=self._download_worker,
                args=(dict(auto_asset), version),
                name="tidoc-core-update-download",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _download_worker(self, asset: dict[str, Any], version: str) -> None:
        try:
            root = self.updates_dir / COMPONENT_CORE / current_platform() / version
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
            filename = str(asset.get("filename") or "tidoc-update.zip")
            archive = root / filename
            partial = archive.with_suffix(archive.suffix + ".part")

            def on_progress(done: int, total: int, elapsed: float) -> None:
                expected_total = total or int(asset.get("size") or 0)
                self._set(
                    progress=min(1.0, done / expected_total) if expected_total else 0.0,
                    downloaded_bytes=done,
                    total_bytes=expected_total,
                    speed_bps=done / elapsed,
                )

            _download_url(str(asset.get("url") or ""), partial, 180, on_progress)
            expected = str(asset.get("sha256") or "").lower()
            self._set(stage="verify")
            actual = sha256_file(partial).lower()
            if not expected or actual != expected:
                raise RuntimeError("更新包完整性校验失败。")
            partial.replace(archive)
            self._set(stage="extract")
            extract_root = root / "staged"
            staged = extract_core_update_archive(
                archive, extract_root,
                root_name=str(asset.get("root_name") or ("tidoc.app" if current_platform() == "macos" else "tidoc")),
            )
            info = {
                "component": COMPONENT_CORE, "version": version,
                "platform": current_platform(), "file_path": str(archive),
                "stage_dir": str(staged), "package_kind": "silent",
                "sha256": actual, "size": archive.stat().st_size,
            }
            marker = _core_update_marker(self.updates_dir)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
            self._set(
                state="ready", progress=1.0, downloaded_bytes=info["size"],
                total_bytes=info["size"], speed_bps=0.0, stage="ready",
                stage_dir=str(staged), file_path=str(archive), error="",
            )
        except Exception as exc:
            self._set(state="failed", progress=0.0, stage="", error=str(exc))
        finally:
            with self._lock:
                self._thread = None

    def install(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state.get("state") != "ready":
            raise RuntimeError("更新包尚未准备好。")
        stage = Path(str(state.get("stage_dir") or ""))
        launch_silent_core_update(
            stage, stage.parents[1], str(state.get("version") or ""),
            current_pid=os.getpid(),
        )
        self._set(state="installing", stage="install")
        return self.status()


def install_print_component(
    manifest: dict[str, Any],
    components_dir: str | Path,
    updates_dir: str | Path,
    plat: str | None = None,
) -> DownloadResult:
    return install_component(manifest, components_dir, updates_dir, COMPONENT_PRINT, plat)


def install_ocr_component(
    manifest: dict[str, Any],
    components_dir: str | Path,
    updates_dir: str | Path,
    plat: str | None = None,
) -> DownloadResult:
    return install_component(manifest, components_dir, updates_dir, COMPONENT_OCR, plat)


def install_component(
    manifest: dict[str, Any],
    components_dir: str | Path,
    updates_dir: str | Path,
    component: str,
    plat: str | None = None,
) -> DownloadResult:
    """下载、校验并安装一个可选组件（打印导出 / OCR 识别共用）。"""
    asset = get_platform_asset(manifest, component, plat)
    downloaded = download_asset(asset, updates_dir)
    install_dir = _component_version_dir(
        components_dir, component, asset.get("version") or "unknown", asset.get("platform") or current_platform()
    )
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    fmt = asset.get("format") or downloaded.suffix.lower().lstrip(".")
    if fmt == "zip":
        with zipfile.ZipFile(downloaded) as zf:
            zf.extractall(install_dir)
    else:
        target = install_dir / (asset.get("executable_name") or downloaded.name)
        shutil.copy2(downloaded, target)
        if asset.get("platform") != "windows":
            target.chmod(target.stat().st_mode | 0o755)

    executable = _find_executable(install_dir, asset)
    if not executable:
        raise RuntimeError("组件已下载，但没有找到可执行文件。")
    if asset.get("platform") != "windows":
        executable.chmod(executable.stat().st_mode | 0o755)

    marker = {
        "component": component,
        "version": asset.get("version") or "",
        "platform": asset.get("platform") or current_platform(),
        "executable": str(executable),
        "source": asset.get("url") or "",
        "sha256": asset.get("sha256") or "",
        "installed_sha256": sha256_file(executable),
    }
    marker_path = _component_root(components_dir, component, asset.get("platform")).joinpath("current.json")
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
    _prune_old_component_versions(
        components_dir, component, marker["platform"], install_dir.name
    )
    return DownloadResult(
        component=component,
        version=marker["version"],
        platform=marker["platform"],
        file_path=downloaded,
        sha256=marker["sha256"],
        size=downloaded.stat().st_size,
        installed_path=executable,
    )


def print_component_executable(components_dir: str | Path, plat: str | None = None) -> Path | None:
    info = installed_component_info(components_dir, COMPONENT_PRINT, plat)
    return Path(info["executable"]) if info["valid"] else None


def _component_root(components_dir: str | Path, component: str, plat: str | None = None) -> Path:
    return Path(components_dir) / component / (plat or current_platform())


def _component_version_dir(components_dir: str | Path, component: str, version: str, plat: str) -> Path:
    return _component_root(components_dir, component, plat) / version


def _prune_old_component_versions(
    components_dir: str | Path, component: str, plat: str, keep_version: str
) -> None:
    """安装成功后清掉同平台其他版本目录，避免历史版本随更新无限累积占磁盘。

    清理尽力而为：个别目录删不掉（如正被占用）时忽略，不影响安装结果；
    残留目录等下次安装再试。
    """
    if not keep_version:
        # 安装目录 fallback 是 unknown/，空 keep 对不上，继续会把刚装上的目录删掉。
        return
    root = _component_root(components_dir, component, plat)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.name == keep_version or not child.is_dir():
            continue
        shutil.rmtree(child, ignore_errors=True)


def _core_update_marker(updates_dir: str | Path, plat: str | None = None) -> Path:
    return Path(updates_dir) / COMPONENT_CORE / (plat or current_platform()) / "current.json"


def _write_core_update_marker(updates_dir: str | Path, asset: dict[str, Any], file_path: Path) -> None:
    marker = _core_update_marker(updates_dir, asset.get("platform"))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "component": COMPONENT_CORE,
        "version": asset.get("version") or "",
        "platform": asset.get("platform") or current_platform(),
        "file_path": str(file_path),
        "filename": file_path.name,
        "source": asset.get("url") or "",
        "sha256": asset.get("sha256") or "",
    }, ensure_ascii=False, indent=2), "utf-8")


def _find_executable(root: Path, asset: dict[str, Any]) -> Path | None:
    preferred = asset.get("executable_name")
    if preferred:
        matches = list(root.rglob(preferred))
        if matches:
            return matches[0]
    suffix = ".exe" if asset.get("platform") == "windows" else ""
    for path in root.rglob("*"):
        if path.is_file() and (suffix and path.name.endswith(suffix) or not suffix and os.access(path, os.X_OK)):
            return path
    files = [p for p in root.rglob("*") if p.is_file()]
    return files[0] if len(files) == 1 else None


def _read_url(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        if _is_certificate_error(exc):
            return _read_url_with_system_trust(url, timeout, exc)
        raise


def _download_url(url: str, out_path: Path, timeout: int, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, out_path.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            started = time.monotonic()
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, max(time.monotonic() - started, 0.001))
    except urllib.error.URLError as exc:
        if _is_certificate_error(exc):
            _download_url_with_system_trust(url, out_path, timeout, exc)
            if progress:
                progress(out_path.stat().st_size, out_path.stat().st_size, 1.0)
            return
        raise


def _is_certificate_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if isinstance(current, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            current = reason
            continue
        return "CERTIFICATE_VERIFY_FAILED" in str(current)
    return False


def _read_url_with_system_trust(url: str, timeout: int, original: BaseException) -> bytes:
    if sys.platform == "darwin":
        cmd = _curl_command(url, timeout)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
        except Exception as exc:
            raise RuntimeError(_cert_fallback_message(original, exc)) from exc
        if proc.returncode == 0:
            return proc.stdout
        err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
        raise RuntimeError(_cert_fallback_message(original, err or f"curl exit {proc.returncode}"))
    raise original


def _download_url_with_system_trust(url: str, out_path: Path, timeout: int, original: BaseException) -> None:
    if sys.platform == "darwin":
        cmd = _curl_command(url, timeout) + ["--output", str(out_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
        except Exception as exc:
            raise RuntimeError(_cert_fallback_message(original, exc)) from exc
        if proc.returncode == 0:
            return
        err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
        raise RuntimeError(_cert_fallback_message(original, err or f"curl exit {proc.returncode}"))
    raise original


def _curl_command(url: str, timeout: int) -> list[str]:
    curl = "/usr/bin/curl" if Path("/usr/bin/curl").exists() else "curl"
    return [
        curl,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
        "--user-agent",
        USER_AGENT,
        url,
    ]


def _cert_fallback_message(original: BaseException, fallback_error: object) -> str:
    return (
        "证书校验失败，已尝试使用系统信任链重新连接但仍失败。"
        f"原始错误：{original}；系统下载错误：{fallback_error}"
    )
