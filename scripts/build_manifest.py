#!/usr/bin/env python3
"""Generate tidoc COS update manifest and upload plan.

Expected release file names:
- tidoc-core-windows-v0.1.20.exe
- tidoc-core-windows-v0.1.21-beta.1.exe  (pre-release: published to manifest-beta.json only)
- tidoc-core-windows-v0.1.20-update.zip
- tidoc-core-macos-v0.1.20.dmg
- tidoc-core-macos-v0.1.20-update.zip
- tidoc-print-windows-v0.1.19.exe
- tidoc-print-macos-v0.1.19.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from release_version import is_prerelease, validate_version

BASE_URL = "https://img.bitfsae.com/tidoc"
STABLE_OUT = "manifest.json"
BETA_OUT = "manifest-beta.json"
DEFAULT_UPLOAD_PLAN = "upload_plan.tsv"
# 版本可带预发布后缀（0.1.39-beta.1）。预发布部分按「尽量短」匹配，所以稳定版的 "-update.zip" 不会被当成预发布后缀；
# 预发布标识只允许字母数字和点（见 release_version.py），否则这里无法和固定的文件后缀区分。
NAME_RE = re.compile(
    r"^tidoc-(?P<component>core|print|ocr)-(?P<platform>windows|macos)-v"
    r"(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*?)??)"
    r"(?P<suffix>-update\.zip|\.exe|\.dmg|\.zip)$"
)

COMPONENT_META = {
    "core": {"name": "tidoc 核心", "entrypoint": "app"},
    "print": {"name": "打印导出组件", "entrypoint": "subprocess"},
    "ocr": {"name": "OCR 识别组件", "entrypoint": "subprocess"},
}

EXECUTABLES = {
    ("core", "windows"): "tidoc.exe",
    ("core", "macos"): "tidoc.app",
    ("print", "windows"): "tidoc_print.exe",
    ("print", "macos"): "tidoc_print",
    ("ocr", "windows"): "tidoc_ocr.exe",
    ("ocr", "macos"): "tidoc_ocr",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", default="release", help="directory containing release files")
    parser.add_argument("--version", required=True, help="release version without leading v")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--out", default=None, help="default: manifest.json, or manifest-beta.json for a pre-release")
    parser.add_argument("--upload-plan", default=DEFAULT_UPLOAD_PLAN)
    parser.add_argument("--notes", default="", help="single line changelog")
    parser.add_argument("--notes-file", default="", help="JSON array of user-facing changelog entries")
    parser.add_argument(
        "--stable-manifest",
        default="",
        help="URL or path of the published stable manifest.json. Required for a pre-release: optional "
        "components (print, OCR) whose version the stable manifest already serves are not republished, "
        "because overwriting a file the stable manifest points at breaks its sha256.",
    )
    parser.add_argument("--min-supported-version", default="0.1.0")
    parser.add_argument("--force-update", action="store_true")
    args = parser.parse_args()

    if args.notes_file:
        notes = json.loads(Path(args.notes_file).read_text("utf-8"))
        if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
            raise SystemExit("--notes-file must contain a JSON array of strings")
        args.release_notes = notes
    else:
        args.release_notes = [args.notes] if args.notes else []

    try:
        validate_version(args.version)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    prerelease = is_prerelease(args.version)
    if args.out is None:
        args.out = BETA_OUT if prerelease else STABLE_OUT
    stable_versions: dict[str, str] = {}
    if prerelease:
        if not args.stable_manifest:
            raise SystemExit(
                "A pre-release needs --stable-manifest so it never overwrites a component file "
                "that the stable manifest still points at."
            )
        stable_versions = _published_versions(args.stable_manifest)
    release_dir = Path(args.release_dir)
    components: dict[str, dict] = {}
    upload_rows: list[tuple[Path, str]] = []

    for path in sorted(p for p in release_dir.iterdir() if p.is_file()):
        match = NAME_RE.match(path.name)
        if not match:
            continue
        info = match.groupdict()
        # The core follows the release tag. Optional components use their own
        # package versions, so an unchanged print component does not become a
        # fake update whenever the core ships.
        if info["component"] == "core" and info["version"] != args.version:
            continue
        component = info["component"]
        if prerelease and component != "core" and stable_versions.get(component) == info["version"]:
            # The stable channel serves this exact file. Releases rebuild the component every time and
            # PyInstaller output is not reproducible, so uploading it again would change the bytes
            # behind the stable manifest's sha256.
            print(f"Skip {path.name}: already published by the stable manifest")
            continue
        platform = info["platform"]
        key = f"tidoc/{component}/{platform}/{path.name}"
        url = f"{args.base_url.rstrip('/')}/{component}/{platform}/{path.name}"
        comp = components.setdefault(
            component,
            _component_block(component, info["version"], args),
        )
        if comp["latest"] != info["version"]:
            raise SystemExit(
                f"Component {component} has inconsistent artifact versions: "
                f"{comp['latest']} and {info['version']}."
            )
        asset = {
            "filename": path.name,
            "url": url,
            "key": key,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "format": _format_for(path),
            "executable_name": EXECUTABLES.get((component, platform), ""),
        }
        if component == "core" and path.name.endswith("-update.zip"):
            asset["root_name"] = "tidoc.app" if platform == "macos" else "tidoc"
            platform_block = comp["platforms"].setdefault(platform, {})
            platform_block["auto_update"] = asset
        else:
            existing_auto = (comp["platforms"].get(platform) or {}).get("auto_update")
            comp["platforms"][platform] = asset
            if existing_auto:
                comp["platforms"][platform]["auto_update"] = existing_auto
        upload_rows.append((path, key))

    if "core" not in components:
        raise SystemExit("No core release files found.")

    manifest = {
        "schema": 1,
        "app": "tidoc",
        "channel": "beta" if prerelease else "stable",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url.rstrip("/"),
        "components": components,
    }
    out_path = release_dir / args.out
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    plan_path = release_dir / args.upload_plan
    plan_lines = [f"{path}\t{key}" for path, key in upload_rows]
    plan_path.write_text("\n".join(plan_lines) + "\n", "utf-8")
    print(f"Wrote {out_path}")
    print(f"Wrote {plan_path}")
    return 0


def _published_versions(source: str) -> dict[str, str]:
    """component -> latest version served by the stable manifest (URL or local path)."""
    try:
        if re.match(r"^https?://", source):
            request = urllib.request.Request(source, headers={"User-Agent": "tidoc-release"})
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        else:
            raw = Path(source).read_bytes()
        components = json.loads(raw.decode("utf-8")).get("components") or {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read the stable manifest {source}: {exc}") from exc
    return {
        name: str(block.get("latest") or "")
        for name, block in components.items()
        if isinstance(block, dict)
    }


def _component_block(component: str, version: str, args) -> dict:
    meta = COMPONENT_META[component]
    return {
        "name": meta["name"],
        "latest": version,
        "min_supported_version": args.min_supported_version,
        "force_update": bool(args.force_update),
        "entrypoint": meta["entrypoint"],
        "release_date": datetime.now(timezone.utc).date().isoformat(),
        "notes": args.release_notes if component == "core" or version == args.version else [],
        "platforms": {},
    }


def _format_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".exe":
        return "exe"
    if suffix == ".dmg":
        return "dmg"
    if suffix == ".zip":
        return "zip"
    return suffix.lstrip(".") or "binary"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
