#!/usr/bin/env python3
"""Release version format shared by the release scripts and the workflow.

A version is ``MAJOR.MINOR.PATCH`` with an optional pre-release suffix made of dot separated
alphanumeric identifiers (``0.1.39-beta.1``, ``0.1.39-rc.2``). Hyphens inside the suffix are not
accepted: the release file names (``...-v0.1.39-beta.1-update.zip``) could no longer be told apart
from the fixed ``-update.zip`` file suffix.
"""

from __future__ import annotations

import re
import sys

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$")


def is_prerelease(version: str) -> bool:
    return "-" in version


def validate_version(version: str) -> str:
    if not VERSION_RE.match(version):
        raise ValueError(
            f"Invalid release version {version!r}: expected 1.2.3 or a pre-release such as "
            "1.2.3-beta.1 / 1.2.3-rc.2 (letters, digits and dots only after the hyphen)."
        )
    return version


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_version.py VERSION", file=sys.stderr)
        return 2
    try:
        validate_version(sys.argv[1].removeprefix("v"))
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
