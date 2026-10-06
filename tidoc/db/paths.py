"""数据根目录与附件仓库的路径管理（设计文档第 5 节存储布局）。

<数据根目录>/
├─ tidoc.sqlite            结构化数据
├─ attachments/<entry_id>/ 附件文件仓库
├─ exports/                导出的绑定包 / 汇总 / 打印件
├─ dropped/                拖拽文件临时中转区
├─ components/             联网下载的可选组件
└─ updates/                核心更新包下载与备份
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "tidoc"


POINTER_NAME = "data-location.txt"


def default_data_root() -> Path:
    """系统应用数据目录下的默认根目录。设置里可改到用户指定位置。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / APP_DIR_NAME
    legacy = base / APP_DIR_NAME.capitalize()
    if legacy.exists() and not target.exists():
        return legacy
    return target


def _pointer_path() -> Path:
    """指针文件固定放在系统默认目录里，记录用户迁移后的真实数据根位置。"""
    return default_data_root() / POINTER_NAME


def resolve_data_root() -> Path:
    """启动时决定实际使用的数据根：有迁移指针且有效则用它，否则用默认目录。"""
    ptr = _pointer_path()
    if ptr.exists():
        try:
            target = ptr.read_text(encoding="utf-8").strip()
            if target and Path(target).exists():
                return Path(target)
        except OSError:
            pass
    return default_data_root()


def set_data_root_pointer(path: str | Path | None) -> None:
    """写入 / 清除迁移指针。path 为空或等于默认目录时清除指针（回到默认）。"""
    ptr = _pointer_path()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    if not path or str(Path(path)) == str(default_data_root()):
        if ptr.exists():
            ptr.unlink()
        return
    ptr.write_text(str(Path(path)), encoding="utf-8")


def ensure_data_root_pointer_writable() -> None:
    """提前验证迁移指针可写，不改变最终指向。"""
    ptr = _pointer_path()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    old = ptr.read_text(encoding="utf-8") if ptr.exists() else None
    ptr.write_text(old or "", encoding="utf-8")
    if old is None:
        ptr.unlink()


class DataRoot:
    """封装一个数据根目录下的所有子路径，并保证目录存在。"""

    def __init__(self, root: str | Path | None = None, *, manage_pointer: bool = False):
        self.root = Path(root) if root else default_data_root()
        self.manage_pointer = manage_pointer
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.dropped_dir.mkdir(parents=True, exist_ok=True)
        self.components_dir.mkdir(parents=True, exist_ok=True)
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.adapter_packages_dir, self.adapter_staging_dir,
                     self.adapter_imports_dir, self.export_jobs_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.root / "tidoc.sqlite"

    @property
    def attachments_dir(self) -> Path:
        return self.root / "attachments"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def dropped_dir(self) -> Path:
        return self.root / "dropped"

    @property
    def components_dir(self) -> Path:
        return self.root / "components"

    @property
    def updates_dir(self) -> Path:
        return self.root / "updates"

    @property
    def adapters_dir(self) -> Path:
        return self.root / "adapters"

    @property
    def adapter_packages_dir(self) -> Path:
        return self.adapters_dir / "packages"

    @property
    def adapter_staging_dir(self) -> Path:
        return self.adapters_dir / "staging"

    @property
    def adapter_imports_dir(self) -> Path:
        return self.adapters_dir / "imports"

    @property
    def export_jobs_dir(self) -> Path:
        return self.root / "export_jobs"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def package_dir(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ValueError("适配包摘要无效。")
        return self.adapter_packages_dir / content_hash

    def job_dir(self, job_id: str) -> Path:
        if not job_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in job_id):
            raise ValueError("导出任务编号无效。")
        return self.export_jobs_dir / job_id

    def entry_dir(self, entry_id: str) -> Path:
        """某个条目的附件目录，按需创建。"""
        path = self.attachments_dir / entry_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def migrate_to(self, new_root: str | Path) -> Path:
        """把整个数据根迁移到用户指定的新位置，并更新迁移指针。

        规则：
        - 目标目录必须为空或不存在（避免覆盖用户已有文件）。
        - 逐项移动数据库与各子目录；WAL 边车文件（-wal/-shm）一并搬。
        - 成功后写指针，返回新根路径。调用方需用新根重建 DataRoot / Database。
        """
        import shutil

        new_root = Path(new_root).expanduser().resolve()
        source = self.root.resolve()
        if new_root == source:
            return self.root
        if source in new_root.parents or new_root in source.parents:
            raise ValueError("新旧数据目录不能互相包含。")
        if new_root.exists() and any(p.name != POINTER_NAME for p in new_root.iterdir()):
            raise ValueError("目标位置不是空目录，请选择一个空文件夹，避免覆盖已有文件。")
        new_root.mkdir(parents=True, exist_ok=True)
        if self.manage_pointer:
            # 先确认指针可写，避免数据已搬走但启动指针没更新的半迁移状态。
            ensure_data_root_pointer_writable()
        moved = []
        try:
            for child in source.iterdir():
                if child.name == POINTER_NAME:
                    continue
                shutil.move(str(child), str(new_root / child.name))
                moved.append(child.name)
            if self.manage_pointer:
                set_data_root_pointer(new_root)
        except BaseException:
            for name in reversed(moved):
                shutil.move(str(new_root / name), str(source / name))
            raise
        return new_root
