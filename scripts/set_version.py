#!/usr/bin/env python3
"""Stamp the core application version for a release build.

The optional print component owns its version in ``tidoc_print/__init__.py``.
Core releases must not bump it unless the component itself changed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from release_notes import bullet_notes, newest_changelog_section


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()
    file = Path("tidoc/__init__.py")
    text = file.read_text("utf-8")
    text = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{args.version}"', text)
    file.write_text(text, "utf-8")
    print(f"set {file} to {args.version}")
    index = Path("tidoc/web/index.html")
    text = index.read_text("utf-8")
    text = re.sub(
        r'((?:styles\.css|api\.js|select\.js|app\.js)\?v=)[^"\']+',
        rf'\g<1>{args.version}',
        text,
    )
    index.write_text(text, "utf-8")
    print(f"set {index} asset version to {args.version}")
    changelog = Path("CHANGELOG.md")
    section = newest_changelog_section(changelog.read_text("utf-8")) if changelog.exists() else ""
    notes = bullet_notes(section)
    release_info = Path("tidoc/release_info.py")
    release_info.write_text(
        '"""由 scripts/set_version.py 生成的当前核心版本发布说明。"""\n\n'
        f'RELEASE_VERSION = {args.version!r}\n'
        f'RELEASE_NOTES = {json.dumps(notes, ensure_ascii=False, indent=2)}\n',
        "utf-8",
    )
    print(f"set {release_info} embedded release notes ({len(notes)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
