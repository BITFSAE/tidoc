"""设置里的抬头与税号配置 + 双击 .tidoc 启动的回归测试。"""

import sys
import tempfile
from pathlib import Path

from tidoc.api import Api

DEFAULT_PROFILES = [
    {"name": "北京理工大学", "tax_id": "12100000400009127B"},
    {"name": "北京理工大学教育基金会", "tax_id": "53100000500021676K"},
]


def test_title_profiles_default_to_builtin_without_saved_config():
    api = Api(tempfile.mkdtemp())

    assert api.title_profiles()["data"]["profiles"] == DEFAULT_PROFILES


def test_set_title_profiles_persists_and_applies_to_new_api():
    root = tempfile.mkdtemp()
    api = Api(root)

    saved = api.set_title_profiles([
        {"name": " 复旦大学 ", "tax_id": "1210 0000-4000 0000 0a"},
        {"name": "复旦大学", "tax_id": "重复项"},
        {"name": "", "tax_id": "x"},
        {"name": "无税号大学"},
    ])
    assert saved["data"]["profiles"] == [
        {"name": "复旦大学", "tax_id": "12100000400000000A"},
        {"name": "无税号大学", "tax_id": ""},
    ]

    fresh = Api(root)
    assert fresh.title_profiles()["data"]["profiles"] == saved["data"]["profiles"]

    cleared = fresh.set_title_profiles([])
    assert cleared["data"]["profiles"] == []
    assert Api(root).title_profiles()["data"]["profiles"] == []


def test_take_launch_file_is_consumed_once():
    api = Api(tempfile.mkdtemp(), launch_file="C:/下载/报账.tidoc")

    assert api.take_launch_file()["data"]["path"] == "C:/下载/报账.tidoc"
    assert api.take_launch_file()["data"]["path"] == ""


def test_secondary_launch_files_are_queued_without_duplicates():
    api = Api(tempfile.mkdtemp())

    api.queue_launch_file("C:/下载/一.tidoc")
    api.queue_launch_file("C:/下载/一.tidoc")
    api.queue_launch_file("C:/下载/二.tidoc")

    assert api.take_launch_file()["data"]["path"] == "C:/下载/一.tidoc"
    assert api.take_launch_file()["data"]["path"] == "C:/下载/二.tidoc"
    assert api.take_launch_file()["data"]["path"] == ""


def test_launch_bindle_path_picked_from_argv(monkeypatch, tmp_path):
    from tidoc.app import _launch_bindle_path

    bindle = tmp_path / "材料包.tidoc"
    bindle.write_bytes(b"x")

    monkeypatch.setattr(sys, "argv", ["tidoc.exe", str(bindle), "--debug"])
    assert _launch_bindle_path() == str(bindle)

    monkeypatch.setattr(sys, "argv", ["tidoc.exe", "--debug", str(tmp_path / "缺失.tidoc")])
    assert _launch_bindle_path() == ""


def test_web_api_surface_has_title_profile_methods():
    source = (Path(__file__).resolve().parents[1] / "tidoc" / "web" / "api.js").read_text("utf-8")

    assert "titleProfiles: () => call('title_profiles')" in source
    assert "setTitleProfiles: (profiles) => call('set_title_profiles', profiles || [])" in source
    assert "takeLaunchFile: () => call('take_launch_file')" in source
