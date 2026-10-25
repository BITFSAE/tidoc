"""用户交付导出文件测试。"""

import zipfile

import pytest

from tidoc.services.exports import export_attachment_zip, export_overview_xlsx


@pytest.mark.parametrize('method,suffix', [
    ('export_bindle', '.tidoc'), ('export_overview_excel', '.xlsx'),
    ('export_attachment_archive', '.zip'),
])
def test_named_exports_keep_existing_files_and_reject_paths(api, method, suffix):
    profile = api.create_profile('示例报账人', '示例审核人')['data']
    eid = api.entries.create(profile['id'])
    export = getattr(api, method)
    first = export([eid], '十月交接')
    assert first['ok'], first
    from pathlib import Path
    path = Path(first['data']['path'])
    before = path.read_bytes()
    assert path.name == '十月交接' + suffix
    duplicate = export([eid], '十月交接')
    assert not duplicate['ok'] and '已存在' in duplicate['error']
    assert path.read_bytes() == before
    invalid = export([eid], '../outside')
    assert not invalid['ok'] and '文件名' in invalid['error']


def test_overview_xlsx_is_valid_zip(repos, tmp_path):
    pid = repos["profiles"].create("张三", "李老师")["id"]
    eid = repos["entries"].create(pid, title="北京理工大学")
    out = export_overview_xlsx(
        repos["entries"],
        {pid: {"name": "张三", "reviewer": "李老师"}},
        [eid],
        tmp_path / "总览.xlsx",
    )

    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "xl/workbook.xml" in names
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "张三" in sheet
        assert "北京理工大学" in sheet


def test_attachment_archive_uses_normalized_names(repos, tmp_path):
    pid = repos["profiles"].create("张三", "李老师")["id"]
    eid = repos["entries"].create(pid, title="北京理工大学")
    src = tmp_path / "乱七八糟的名字.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    repos["attachments"].add(eid, src, "invoice_pdf")

    out = export_attachment_zip(
        repos["entries"],
        repos["root"].attachments_dir,
        {pid: {"name": "张三", "reviewer": "李老师"}},
        [eid],
        tmp_path / "附件.zip",
    )

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "清单.txt" in names
        assert any(name.endswith("/发票_01.pdf") for name in names)
