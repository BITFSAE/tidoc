"""截图用的虚构数据：8 条条目、2 位报账人、2 个批次，全部写进隔离数据目录。

这里没有真实发票、姓名、税号或账号。抬头沿用内置 BITFSAE 方案里的两个公开抬头。
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter

PACKAGE_ID = "org.bitfsae.reimbursement"
UNIVERSITY = "北京理工大学"
FOUNDATION = "北京理工大学教育基金会"
TAX_IDS = {UNIVERSITY: "12100000400009127B", FOUNDATION: "53100000500021676K"}

# 附件代码：i 发票 PDF，p 付款截图（可重复），q 查验单，x 实物图。
# 字段顺序：编号、物资、金额、报账人、抬头、批次、销售方、日期、备注、标签、附件、识别提醒、实付。
ROWS = [
    (101, "A4复印纸", "345.96", "张三", UNIVERSITY, 1, "示例办公用品有限公司", "2026-09-12", "含复印纸和中性笔两项，数量已核对", "办公耗材", "ipqx", None, None),
    (102, "活页笔记本", "120.01", "张三", UNIVERSITY, 1, "示例文具商行", "2026-09-13", "", "办公耗材", "ipq", None, None),
    (103, "超五类网线", "400.00", "李四", UNIVERSITY, 1, "示例电子科技有限公司", "2026-09-14", "付款截图还没拿到，周五前补", "实验室", "i", None, None),
    (104, "螺丝刀套装", "90.00", "李四", FOUNDATION, 1, "示例五金工具有限公司", "2026-09-15", "", "", "ipq", None, None),
    (105, "打包胶带", "156.00", "张三", UNIVERSITY, 2, "示例包装耗材有限公司", "2026-09-16", "销售方开错了购买方税号，已联系重开", "", "ipq", "购买方税号与抬头不一致", None),
    (106, "锂电池组", "837.89", "张三", UNIVERSITY, 2, "示例能源设备有限公司", "2026-09-17", "券后实付 820.00；XML 漏一条明细", "电气", "ipqx", "XML 明细合计与票面金额不一致", "820.00"),
    (107, "U盘", "200.01", "张三", UNIVERSITY, 2, "示例数码商城", "2026-09-18", "", "", "ipp", None, None),
    (108, "数显游标卡尺", "260.01", "李四", FOUNDATION, None, "示例机电设备有限公司", "2026-09-19", "", "实验室", "iqx", None, None),
]
BATCH_NAMES = {1: "9 月第一批", 2: "9 月第二批"}


class Backend:
    """Api 的返回值统一是 {ok, data|error}；这里解开，出错直接抛异常。"""

    def __init__(self, api):
        self._api = api

    def __getattr__(self, name):
        value = getattr(self._api, name)
        if not callable(value) or name.startswith("_"):
            return value

        def call(*args, **kwargs):
            result = value(*args, **kwargs)
            if isinstance(result, dict) and "ok" in result:
                if not result["ok"]:
                    raise RuntimeError(f"{name}: {result.get('error')}")
                return result.get("data")
            return result

        return call


def _invoice_xml(path: Path, number: str, date: str, seller: str, title: str, item: str, amount: str) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<EInvoice><Header><EIid>{number}</EIid></Header><EInvoiceData>"
        f"<SellerInformation><SellerName>{seller}</SellerName></SellerInformation>"
        f"<BuyerInformation><BuyerName>{title}</BuyerName><BuyerIdNum>{TAX_IDS[title]}</BuyerIdNum></BuyerInformation>"
        f"<BasicInformation><TotalTax-includedAmount>{amount}</TotalTax-includedAmount>"
        f"<RequestTime>{date} 10:00:00</RequestTime></BasicInformation>"
        f"<IssuItemInformation><ItemName>{item}</ItemName><Quantity>1</Quantity>"
        f"<Amount>{amount}</Amount><ComTaxAm>0</ComTaxAm></IssuItemInformation>"
        "</EInvoiceData></EInvoice>",
        encoding="utf-8",
    )


def _blank_pdf(path: Path, size: int) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=size, height=size)
    with open(path, "wb") as handle:
        writer.write(handle)


def _blank_png(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (40, 40), color).save(path)


def seed(api, work_dir: Path) -> None:
    """按 ROWS 创建条目。附件内容各不相同，避开重复文件检查。"""
    from tidoc.db import TYPE_INSPECTION, TYPE_INVOICE_PDF, TYPE_PAYMENT, TYPE_PHYSICAL_IMAGE

    backend = Backend(api)
    scheme = next(item for item in backend.list_schemes() if item["package_id"] == PACKAGE_ID)
    backend.complete_adapter_setup(scheme["id"])
    # 卡片上的报账人、批次徽标需要多位报账人和多个批次才会出现。
    profiles = {
        "张三": backend.create_profile("张三", "王老师", True)["id"],
        "李四": backend.create_profile("李四", "王老师", False)["id"],
    }
    batches = {key: backend.create_batch(name)["id"] for key, name in BATCH_NAMES.items()}

    work_dir.mkdir(parents=True, exist_ok=True)
    counter = itertools.count(1)
    # 列表按最近更新排序：从最后一条开始建，让 101 排在最前。
    for number, item, amount, person, title, batch, seller, date, note, tag, files, warning, paid in reversed(ROWS):
        invoice_no = f"26999000000000000{number}"
        xml_path = work_dir / f"{invoice_no}.xml"
        _invoice_xml(xml_path, invoice_no, date, seller, title, item, amount)
        entry_id = backend.create_entry(profiles[person], title=title, xml_path=str(xml_path))["id"]
        if batch:
            backend.add_entries_to_batch(batches[batch], [entry_id])
        for code in files:
            n = next(counter)
            if code == "i":
                path = work_dir / f"invoice{n}.pdf"
                _blank_pdf(path, 200 + n)
                api.attachments.add(entry_id, str(path), TYPE_INVOICE_PDF)
            elif code == "q":
                path = work_dir / f"inspection{n}.pdf"
                _blank_pdf(path, 300 + n)
                api.attachments.add(entry_id, str(path), TYPE_INSPECTION)
            else:
                path = work_dir / f"image{n}.png"
                _blank_png(path, (200, 100 + n, 100) if code == "p" else (100, 100 + n, 200))
                api.attachments.add(entry_id, str(path), TYPE_PAYMENT if code == "p" else TYPE_PHYSICAL_IMAGE)
        if note:
            backend.update_field(entry_id, "notes", note)
        if tag:
            backend.add_tag([entry_id], tag)
        if paid:
            backend.update_field(entry_id, "paid_amount", paid)
        if warning:
            api.entries.set_check(entry_id, "warning", warning)
        api.entries.recompute_status(entry_id)
        time.sleep(1.1)  # 更新时间精确到秒，间隔一秒以上排序才稳定
