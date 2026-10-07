"""Repository-level guards for problems that only show up on other platforms or locales."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_requirements_files_are_ascii_so_pip_reads_them_under_any_locale():
    # pip decodes requirements files with the locale code page unless a BOM or coding comment says
    # otherwise. A Chinese comment makes `pip install -r` fail with UnicodeDecodeError on a GBK
    # Windows, while the English-locale CI runners never notice.
    for path in sorted(ROOT.glob('requirements*.txt')):
        try:
            path.read_bytes().decode('ascii')
        except UnicodeDecodeError as exc:
            raise AssertionError(f'{path.name} must be ASCII-only (byte {exc.start}); translate the comment') from exc
