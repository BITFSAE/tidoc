"""打印导出编排（设计文档第 9 节）。

对外主入口 build_print_package：
- 输入一组 PrintEntry（可跨人）+ 选项。
- 按抬头强隔离分组，每个抬头单独出一套文件，绝不混合（第 7 节）。
- 默认按条目连续生成材料拼接 PDF；也可按材料类型分别生成旧式拼接 PDF。
- 拼接页只叠加份数 / 页码编号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import PersonProfile, PrintEntry
from .pdf_merge import images_to_pdf, merge_pdf_groups, merge_pdfs
from .word_docs import generate_acceptance_doc, generate_reimburse_doc

# 旧配置保留兼容；当前打印件只使用固定编号，不再展示发票号/姓名/金额。
ANNOTATION_FIELDS = ("invoice_no", "invoice_date", "seller", "person_name", "paid_amount", "total")
_ANNOTATION_LABEL = {
    "invoice_no": "发票号", "invoice_date": "日期", "seller": "销售方",
    "person_name": "报账人", "paid_amount": "实付", "total": "价税合计",
}


@dataclass
class PrintOptions:
    document_date: str = ""                       # 默认取今天
    storage_location: str = ""
    annotate: bool = True                         # 拼接页是否叠加信息
    annotation_fields: tuple[str, ...] = ("invoice_no", "person_name", "paid_amount")
    batch_note: str = ""
    make_entry_bundle_pdf: bool = True
    make_invoice_pdf: bool = False
    make_payment_pdf: bool = False
    make_inspection_pdf: bool = False
    make_reimburse_doc: bool = True
    make_acceptance_doc: bool = True

    def __post_init__(self):
        if not self.document_date:
            now = datetime.now()
            self.document_date = f"{now.year}年{now.month}月{now.day}日"


@dataclass
class PrintResult:
    title: str
    files: dict[str, str] = field(default_factory=dict)   # 文件类型 -> 路径


def _annotation_for(entry: PrintEntry, fields: tuple[str, ...]) -> str:
    """按勾选字段拼出一条页脚标注文字。"""
    parts = []
    for f in fields:
        if f == "person_name":
            val = entry.profile_name
        elif f == "total":
            val = f"¥{entry.total}"
        else:
            val = getattr(entry, f, "")
        if val:
            parts.append(f"{_ANNOTATION_LABEL.get(f, f)}：{val}")
    return "   ".join(parts)


def _safe_name(text: str) -> str:
    import re
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", text).strip()
    return cleaned or "未命名"


def build_print_package(
    entries: list[PrintEntry],
    out_dir: str | Path,
    options: PrintOptions | None = None,
    profiles: dict[str, PersonProfile] | None = None,
) -> list[PrintResult]:
    """按抬头强隔离，为每个抬头生成一套打印件。返回每个抬头的结果。"""
    from dataclasses import asdict
    from .context import json_data
    from .protocol import convert_v1_request, render_request
    request = convert_v1_request({
        "entries": [json_data(asdict(entry)) for entry in entries],
        "profiles": {key: json_data(asdict(value)) for key, value in (profiles or {}).items()},
        "options": json_data(asdict(options or PrintOptions())),
        "out_dir": str(out_dir),
    })
    # Old callers passed an already-created parent directory. Publish only fresh
    # subject directories after one v2 job has rendered every selected file.
    target = Path(out_dir)
    if target.exists():
        import shutil
        import uuid
        request['output_dir'] = str(target.parent / ('.tidoc-legacy-' + uuid.uuid4().hex))
        result = render_request(request)
        staging = Path(request['output_dir'])
        try:
            for child in staging.iterdir():
                if (target / child.name).exists():
                    raise ValueError('输出目录已存在，不能覆盖交付文件')
            for child in list(staging.iterdir()):
                child.rename(target / child.name)
            for row in result['results']:
                row['files'] = {key: str(target / Path(value).relative_to(staging)) for key,value in row['files'].items()}
        finally:
            shutil.rmtree(staging,ignore_errors=True)
    else:
        result = render_request(request)
    return [PrintResult(title=row["title"], files=row["files"]) for row in result["results"]]
