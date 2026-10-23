"""HMAC 签名（设计文档第 6.1 节）。

改动检测：密钥内置于软件，对绑定包内每个文件与汇总文本算 HMAC-SHA256，
生成校验清单。导入时逐项校验，不符即判定"已被外部修改"，醒目标红，不静默接受。
密钥随开源代码公开，所以这只能发现手工改动和文件损坏，不能防止有意伪造，也不是加密。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import BinaryIO
from pathlib import Path

# 内置密钥。注意：这是检测级方案，密钥随软件分发，能识别手工乱改即达标。
_BUILTIN_KEY = b"tidoc-v1-builtin-hmac-key-do-not-rely-for-secrecy"

MANIFEST_NAME = "signatures.json"


def sign_bytes(data: bytes, key: bytes = _BUILTIN_KEY) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def sign_file(path: str | Path, key: bytes = _BUILTIN_KEY) -> str:
    h = hmac.new(key, digestmod=hashlib.sha256)
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sign_stream(stream: BinaryIO, key: bytes = _BUILTIN_KEY) -> str:
    """Calculate a signature without loading a large archive member into memory."""
    digest = hmac.new(key, digestmod=hashlib.sha256)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def verify(data: bytes, signature: str, key: bytes = _BUILTIN_KEY) -> bool:
    """比较签名。"""
    return hmac.compare_digest(sign_bytes(data, key), signature)


def verify_stream(
    stream: BinaryIO, signature: str, key: bytes = _BUILTIN_KEY
) -> bool:
    """Verify an archive member incrementally to cap memory and allocation cost."""
    return hmac.compare_digest(sign_stream(stream, key), signature)
