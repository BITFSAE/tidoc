"""拖入文件改用原生路径：后端按名字认领、流式复制到中转区，不再经页面读取内容。"""

import threading
from pathlib import Path

import pytest

from tidoc.api import Api
from tidoc.services.native_drop import NativeDropRegistry


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def drop_event(*files):
    return {"type": "drop", "dataTransfer": {"files": [
        {"name": name, "size": size, "pywebviewFullPath": str(path)} for name, size, path in files
    ]}}


@pytest.fixture
def registry():
    result = NativeDropRegistry()
    result.enabled = True
    return result


def test_recorded_paths_are_claimed_by_name_and_size(registry, tmp_path):
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.png"
    first.write_bytes(b"1234")
    second.write_bytes(b"12")
    registry.record(drop_event(("a.pdf", 4, first), ("b.png", 2, second)))
    claimed = registry.claim([{"name": "b.png", "size": 2}, {"name": "a.pdf", "size": 4}])
    assert claimed == [str(second), str(first)]
    assert registry.claim([{"name": "a.pdf", "size": 4}], timeout=0) is None   # 每条记录只能认领一次


def test_same_name_with_a_different_size_is_a_different_file(registry, tmp_path):
    file = tmp_path / "scan.pdf"
    file.write_bytes(b"1234")
    registry.record(drop_event(("scan.pdf", 4, file)))
    assert registry.claim([{"name": "scan.pdf", "size": 99}], timeout=0) is None
    assert registry.claim([{"name": "scan.pdf", "size": 4}], timeout=0) == [str(file)]


def test_files_without_native_paths_are_not_recorded(registry):
    registry.record({"dataTransfer": {"files": [{"name": "a.pdf", "size": 1}]}})
    registry.record({"dataTransfer": None})
    registry.record(None)
    assert registry.claim([{"name": "a.pdf", "size": 1}], timeout=0) is None


def test_unreadable_or_missing_files_fall_back_to_reading_content(registry, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    registry.record(drop_event(("folder", 0, folder), ("gone.pdf", 1, tmp_path / "gone.pdf")))
    assert registry.claim([{"name": "folder", "size": 0}], timeout=0) is None
    assert registry.claim([{"name": "gone.pdf", "size": 1}], timeout=0) is None


def test_claim_waits_for_a_late_event_but_not_when_disabled(registry, tmp_path):
    file = tmp_path / "late.pdf"
    file.write_bytes(b"x")
    timer = threading.Timer(0.05, registry.record, args=(drop_event(("late.pdf", 1, file)),))
    timer.start()
    assert registry.claim([{"name": "late.pdf", "size": 1}], timeout=2) == [str(file)]
    timer.join()
    registry.enabled = False
    registry.record(drop_event(("late.pdf", 1, file)))
    assert registry.claim([{"name": "late.pdf", "size": 1}], timeout=2) is None


def test_old_records_expire_and_the_list_stays_bounded(tmp_path):
    clock = Clock()
    registry = NativeDropRegistry(clock=clock, lifetime=60, max_records=3)
    registry.enabled = True
    file = tmp_path / "x.pdf"
    file.write_bytes(b"x")
    registry.record(drop_event(("old.pdf", 1, file)))
    clock.now = 61
    registry.record(drop_event(*[(f"n{index}.pdf", 1, file) for index in range(5)]))
    assert registry.claim([{"name": "old.pdf", "size": 1}], timeout=0) is None
    assert registry.claim([{"name": "n0.pdf", "size": 1}], timeout=0) is None      # 超出上限的旧记录被丢弃
    assert registry.claim([{"name": "n4.pdf", "size": 1}], timeout=0) == [str(file)]


def test_api_copies_native_files_into_the_staging_folder(tmp_path):
    api = Api(tmp_path / "data")
    source = tmp_path / "发票 样例.pdf"
    source.write_bytes(b"%PDF-1.4" * 1000)
    api._native_drops.enabled = True
    api._native_drops.record(drop_event((source.name, source.stat().st_size, source)))

    result = api.take_dropped_paths([{"name": source.name, "size": source.stat().st_size}])

    assert result["ok"]
    [staged] = result["data"]["paths"]
    assert staged != str(source)
    assert staged.startswith(str(api.data_root.dropped_dir))
    assert Path(staged).read_bytes() == source.read_bytes()
    assert api.cleanup_dropped_files([staged])["data"]["deleted"] == 1
    assert source.exists()                                                            # 原文件不动


def test_api_without_native_paths_asks_the_page_to_read_the_content(tmp_path):
    api = Api(tmp_path / "data")
    api._native_drops.enabled = True
    result = api.take_dropped_paths([{"name": "unknown.pdf", "size": 1}])
    assert result["data"] == {"paths": None}


def test_api_keeps_the_size_limits_and_leaves_nothing_behind(tmp_path, monkeypatch):
    import tidoc.api as api_module

    api = Api(tmp_path / "data")
    small = tmp_path / "small.pdf"
    big = tmp_path / "big.pdf"
    small.write_bytes(b"x" * 10)
    big.write_bytes(b"x" * 30)
    monkeypatch.setattr(api_module, "MAX_DROPPED_FILE_BYTES", 20)
    api._native_drops.enabled = True
    api._native_drops.record(drop_event(("small.pdf", 10, small), ("big.pdf", 30, big)))

    result = api.take_dropped_paths([{"name": "small.pdf", "size": 10}, {"name": "big.pdf", "size": 30}])

    assert not result["ok"] and "big.pdf" in result["error"]
    assert not any(api.data_root.dropped_dir.rglob("*.pdf"))                          # 已复制的也清掉
