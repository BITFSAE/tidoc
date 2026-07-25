#!/usr/bin/env python3
"""Stamp the core application version for a release build.

The optional print component owns its version in ``tidoc_print/__init__.py``.
Core releases must not bump it unless the component itself changed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


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
        r'((?:styles\.css|api\.js|app\.js)\?v=)[^"\']+',
        rf'\g<1>{args.version}',
        text,
    )
    index.write_text(text, "utf-8")
    print(f"set {index} asset version to {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
