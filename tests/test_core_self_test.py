"""核心 --self-test 的二维码识别库检查。

自检在打包后的成品上运行：它必须真的把一张程序内生成的虚构二维码编码、存成图片、再解出来，
只检查 import 发现不了缺失的原生库。这里用源码环境验证自检的判定逻辑和失败时的退出码，
全程不联网。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tidoc import app
from tidoc.services import qr_fetch

ROOT = Path(__file__).resolve().parents[1]
SELF_TEST_TEXT = "https://example.invalid/x"


@pytest.fixture
def offline(monkeypatch):
    """自检不得发出任何网络连接或域名解析。"""

    def refuse(*_args, **_kwargs):
        raise AssertionError("the core self-test must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_core_self_test_round_trips_a_qr_code_without_network(offline):
    app.self_test()


def test_qr_check_decodes_the_fictional_code_from_png_and_jpeg(monkeypatch):
    seen = []
    real_decode = qr_fetch.decode_qr_texts

    def spy(data, accept=None):
        texts = real_decode(data, accept)
        seen.append((data[:2], texts))
        return texts

    monkeypatch.setattr(qr_fetch, "decode_qr_texts", spy)

    app._verify_qr_decoder()

    assert seen == [
        (b"\x89P", [SELF_TEST_TEXT]),      # PNG signature
        (b"\xff\xd8", [SELF_TEST_TEXT]),     # JPEG start-of-image
    ]


@pytest.mark.parametrize(
    "decoded",
    [[], ["https://example.invalid/other"], [SELF_TEST_TEXT, "https://example.invalid/extra"]],
)
def test_qr_check_fails_when_the_decoded_text_is_wrong(monkeypatch, decoded):
    monkeypatch.setattr(qr_fetch, "decode_qr_texts", lambda data, accept=None: decoded)

    with pytest.raises(RuntimeError, match="unexpected result"):
        app._verify_qr_decoder()


@pytest.mark.parametrize("code", ["image_unreadable", "decoder_unavailable"])
def test_qr_check_fails_when_the_decoder_reports_an_error(monkeypatch, code):
    def fail(data, accept=None):
        raise qr_fetch.QrFetchError(code)

    monkeypatch.setattr(qr_fetch, "decode_qr_texts", fail)

    with pytest.raises(RuntimeError, match=code):
        app._verify_qr_decoder()


@pytest.mark.parametrize("module", ["zxingcpp", "PIL"])
def test_qr_check_fails_when_a_library_cannot_be_loaded(monkeypatch, module):
    # A None entry makes `import <module>` raise ImportError, like a library missing from the bundle.
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(RuntimeError, match="cannot be loaded"):
        app._verify_qr_decoder()


def test_failed_self_test_exits_with_status_one_instead_of_raising(monkeypatch, capsys):
    # An uncaught exception in a windowed build opens a blocking error dialog, so main() must turn it
    # into an exit status. The cause still goes to stderr for runs that have one.
    monkeypatch.setattr(qr_fetch, "decode_qr_texts", lambda data, accept=None: [])
    monkeypatch.setattr(sys, "argv", ["tidoc", "--self-test"])
    # main() must return before it creates the single-instance lock or any window.
    monkeypatch.setattr(app, "SingleInstance", lambda: pytest.fail("self-test must not start the app"))

    with pytest.raises(SystemExit) as exit_info:
        app.main()

    assert exit_info.value.code == 1
    assert "unexpected result" in capsys.readouterr().err


def test_failed_self_test_still_exits_when_there_is_no_stderr(monkeypatch):
    # Windowed Windows builds run with sys.stderr set to None.
    monkeypatch.setattr(qr_fetch, "decode_qr_texts", lambda data, accept=None: [])
    monkeypatch.setattr(sys, "argv", ["tidoc", "--self-test"])
    monkeypatch.setattr(sys, "stderr", None)

    with pytest.raises(SystemExit) as exit_info:
        app.main()

    assert exit_info.value.code == 1


def test_self_test_flag_returns_quietly_when_everything_loads(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tidoc", "--self-test"])
    monkeypatch.setattr(app, "SingleInstance", lambda: pytest.fail("self-test must not start the app"))

    assert app.main() is None


def test_self_test_process_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "tidoc", "--self-test"],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
    )

    assert result.returncode == 0, result.stderr


def test_self_test_process_exits_nonzero_when_the_qr_library_is_missing():
    script = (
        "import sys; sys.modules['zxingcpp'] = None; sys.argv = ['tidoc', '--self-test']; "
        "from tidoc.app import main; main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
    )

    assert result.returncode != 0
    assert "QR decoder libraries cannot be loaded" in result.stderr
