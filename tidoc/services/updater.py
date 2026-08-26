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
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tidoc import __version__ as CORE_VERSION

MANIFEST_URL = "https://img.bitfsae.com/tidoc/manifest.json"
USER_AGENT = f"tidoc/{CORE_VERSION}"
COMPONENT_PRINT = "print"
COMPONENT_CORE = "core"


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
    file_path = Path(info.get("file_path") or "")
    if not file_path.exists():
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
    for name in (COMPONENT_CORE, COMPONENT_PRINT):
        try:
            asset = get_platform_asset(manifest, name, plat)
        except KeyError:
            continue
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
            "name": asset.get("name") or name,
            "current_version": component_info.get("version", current),
            "latest_version": latest,
            "available": available,
            "installed_valid": component_info.get("valid", True),
            "needs_repair": component_info.get("needs_repair", False),
            "install_issue": component_info.get("issue", ""),
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


def install_print_component(
    manifest: dict[str, Any],
    components_dir: str | Path,
    updates_dir: str | Path,
    plat: str | None = None,
) -> DownloadResult:
    asset = get_platform_asset(manifest, COMPONENT_PRINT, plat)
    downloaded = download_asset(asset, updates_dir)
    install_dir = _component_version_dir(
        components_dir, COMPONENT_PRINT, asset.get("version") or "unknown", asset.get("platform") or current_platform()
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
        raise RuntimeError("打印组件已下载，但没有找到可执行文件。")
    if asset.get("platform") != "windows":
        executable.chmod(executable.stat().st_mode | 0o755)

    marker = {
        "component": COMPONENT_PRINT,
        "version": asset.get("version") or "",
        "platform": asset.get("platform") or current_platform(),
        "executable": str(executable),
        "source": asset.get("url") or "",
        "sha256": asset.get("sha256") or "",
        "installed_sha256": sha256_file(executable),
    }
    marker_path = _component_root(components_dir, COMPONENT_PRINT, asset.get("platform")).joinpath("current.json")
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
    _prune_old_component_versions(
        components_dir, COMPONENT_PRINT, marker["platform"], install_dir.name
    )
    return DownloadResult(
        component=COMPONENT_PRINT,
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


def _download_url(url: str, out_path: Path, timeout: int) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, out_path.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 1024)
    except urllib.error.URLError as exc:
        if _is_certificate_error(exc):
            _download_url_with_system_trust(url, out_path, timeout, exc)
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
