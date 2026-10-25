"""Validate operator-supplied export names before writing any files."""
import re
from pathlib import Path


def export_filename(stem: str, suffix: str) -> str:
    if not isinstance(stem, str):
        raise ValueError("文件名必须是文字。")
    stem = stem.strip()
    if not stem or stem in (".", ".."):
        raise ValueError("请填写文件名。")
    if len(stem) > 120 or len((stem + suffix).encode("utf-8")) > 240:
        raise ValueError("文件名过长，请缩短后重试。")
    if re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', stem) or stem.endswith("."):
        raise ValueError('文件名不能包含 < > : " / \\ | ? * 或控制字符，也不能以句点结尾。')
    if stem.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10))}:
        raise ValueError("这个文件名是系统保留名称，请换一个。")
    return stem + suffix


def export_path(directory: Path, stem: str, suffix: str) -> Path:
    path = directory / export_filename(stem, suffix)
    if path.exists():
        raise ValueError(f"导出目录已存在「{path.name}」，请修改文件名后重试。")
    return path
