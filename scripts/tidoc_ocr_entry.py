"""PyInstaller entrypoint for the external OCR component."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tidoc_ocr.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
