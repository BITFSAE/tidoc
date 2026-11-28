"""截图用的虚构数据：8 条条目、2 位报账人、2 个批次，导入预览用的文件和绑定包，全部写进隔离数据目录。

这里没有真实发票、姓名、税号或账号。抬头沿用内置 BITFSAE 方案里的两个公开抬头。
"""

from __future__ import annotations

import itertools
import json
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
    (105, "打包胶带", "156.00", "张三", UNIVERSITY, 2, "示例包装耗材有限公司", "2026-09-16", "销售方开错了购买方税号，已联系重开", "", "ipq", "[抬头税号] 购买方税号与抬头不一致，请核对。", None),
    (106, "锂电池组", "837.89", "张三", UNIVERSITY, 2, "示例能源设备有限公司", "2026-09-17", "券后实付 820.00；XML 漏一条明细", "电气", "ipqx",
     "[发票金额] 明细识别合计与发票总额相差 17.89，可能是明细识别不完整，请以发票总额为准。；"
     "[发票信息] 销售方名称识别到两个不同的结果：「示例能源设备有限公司」与「示例能源设备有限公 司」，"
     "已采用「示例能源设备有限公司」，请核对。；"
     "[付款截图识别] 付款截图识别到 820.00，但截图里有优惠券抵扣，请核对。", "820.00"),
    (107, "U盘", "200.01", "张三", UNIVERSITY, 2, "示例数码商城", "2026-09-18", "", "", "ipp", None, None),
    (108, "数显游标卡尺", "260.01", "李四", FOUNDATION, None, "示例机电设备有限公司", "2026-09-19", "", "实验室", "iqx", None, None),
]
# 已标为已核对的提醒：106 的备注写明了券后实付，付款截图这条核对过，详情截图里灰显。
REVIEWED = {106: ["付款截图识别到 820.00，但截图里有优惠券抵扣，请核对。"]}

BATCH_NAMES = {1: "9 月第一批", 2: "9 月第二批"}
# 批次的示例备注，让「编辑批次」截图里能看到一段真实的填写内容。
BATCH_NOTES = {1: "9 月上旬采购，月底前报账"}


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


def _text_pdf(path: Path, lines: list[str]) -> None:
    """写一页只含纯文本的 PDF，用来让导入扫描从正文读到发票号。

    手写对象表而不依赖额外库：截图脚本只需要能被 pypdf 读出文字的页面。
    """
    operations = ["BT", "/F1 12 Tf", "72 760 Td"]
    for index, line in enumerate(lines):
        if index:
            operations.append("0 -18 Td")
        operations.append(f"({line}) Tj")
    operations.append("ET")
    stream = "\n".join(operations).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def _record_first_check(api, attachment: dict, invoice_no: str) -> None:
    """虚构的查验单是空白 PDF，读不出查验次数，卡片会出现提醒；这里写入「当日第 1 次」的读取结果，
    让截图里的查验单是正常状态（与软件读到真实查验单正文后保存的内容相同）。"""
    from tidoc.services.inspection_pdf import INSPECTION_RECOGNITION_VERSION

    info = {"invoice_no": invoice_no, "count": 1, "count_scope": "day",
            "checked_at": "2026-09-20 10:00:00", "sha256": attachment["sha256"]}
    api.db.conn.execute(
        "UPDATE attachments SET recognition_version=?, recognition_status='recognized', recognized_value=? WHERE id=?",
        (INSPECTION_RECOGNITION_VERSION, json.dumps(info, ensure_ascii=False), attachment["id"]),
    )
    api.db.conn.commit()


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
    batches = {key: backend.create_batch(name, BATCH_NOTES.get(key, ""))["id"] for key, name in BATCH_NAMES.items()}

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
                _record_first_check(api, api.attachments.add(entry_id, str(path), TYPE_INSPECTION), invoice_no)
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
        if number in REVIEWED:
            api.entries.review_check_lines(entry_id, REVIEWED[number])
        api.entries.recompute_status(entry_id)
        time.sleep(1.1)  # 更新时间精确到秒，间隔一秒以上排序才稳定


def seed_bindle_preview(api, work_dir: Path) -> Path:
    """构造同时包含新增、待补充和无变化条目的虚构绑定包。"""
    from tidoc.db import TYPE_INSPECTION
    from tidoc.services.bindle import export_bindle

    backend = Backend(api)
    entries = {entry["invoice_no"]: entry for entry in api.entries.list()}
    existing_ids = [entries[f"26999000000000000{n}"]["id"] for n in (101, 102)]
    profiles = {profile["id"]: profile for profile in api.profiles.list()}
    profile_ids = {profile["name"]: profile["id"] for profile in profiles.values()}
    # 包里多一张查验单的条目显示为「已有·待补充」；临时新建、导出后删掉的条目显示为「新增」。
    new_ids = []
    for n, name, amount, person in ((201, "文件收纳盒", "48.00", "张三"), (202, "标签纸", "26.50", "李四")):
        xml_path = work_dir / f"preview-{n}.xml"
        _invoice_xml(xml_path, f"26999000000000000{n}", "2026-09-20", "示例办公用品商行", UNIVERSITY, name, amount)
        new_ids.append(backend.create_entry(profile_ids[person], title=UNIVERSITY, xml_path=str(xml_path))["id"])
    extra = work_dir / "补交查验单.pdf"
    _blank_pdf(extra, 600)
    attachment = api.attachments.add(existing_ids[0], str(extra), TYPE_INSPECTION)
    package = export_bindle(api.entries, api.attachments, existing_ids + new_ids,
                            work_dir / "9月补交材料.tidoc", profiles)
    api.attachments.delete(attachment["id"])
    api.entries.recompute_status(existing_ids[0])
    for entry_id in new_ids:
        backend.delete_entry(entry_id)
    return package


def seed_bindle_batch(api, work_dir: Path) -> list[Path]:
    """构造 3 个虚构绑定包用于批量导入截图：纯新增、含已有待补充、完整性异常（需单包预览）。"""
    import zipfile
    from tidoc.db import TYPE_INSPECTION
    from tidoc.services.bindle import export_bindle

    work_dir.mkdir(parents=True, exist_ok=True)
    backend = Backend(api)
    entries = {entry["invoice_no"]: entry for entry in api.entries.list()}
    existing_ids = [entries[f"26999000000000000{n}"]["id"] for n in (101, 102)]
    profiles = {profile["id"]: profile for profile in api.profiles.list()}
    profile_ids = {profile["name"]: profile["id"] for profile in profiles.values()}

    def make_temp_entries(specs):
        created = []
        for n, name, amount, person, seller in specs:
            xml_path = work_dir / f"batch-{n}.xml"
            _invoice_xml(xml_path, f"26999000000000000{n}", "2026-09-21", seller, UNIVERSITY, name, amount)
            created.append(backend.create_entry(profile_ids[person], title=UNIVERSITY, xml_path=str(xml_path))["id"])
        return created

    # 包 1：电控组九月材料（2 条新增，可直接导入）
    ids1 = make_temp_entries([
        (401, "杜邦线套装", "128.00", "张三", "示例电子元器件商行"),
        (402, "接线端子", "215.50", "李四", "示例工业电气有限公司"),
    ])
    pkg1 = export_bindle(api.entries, api.attachments, ids1, work_dir / "电控组九月材料.tidoc", profiles)
    for eid in ids1:
        backend.delete_entry(eid)

    # 包 2：加工费补充材料（1 条已有待补充 + 1 条已有无变化，可直接导入）
    extra = work_dir / "批量补交查验单.pdf"
    _blank_pdf(extra, 610)
    attachment = api.attachments.add(existing_ids[0], str(extra), TYPE_INSPECTION)
    pkg2 = export_bindle(api.entries, api.attachments, existing_ids, work_dir / "加工费补充材料.tidoc", profiles)
    api.attachments.delete(attachment["id"])
    api.entries.recompute_status(existing_ids[0])

    # 包 3：采购组报账（1 条新增，但签名清单被改动，触发「需核对」）
    ids3 = make_temp_entries([
        (403, "铝型材角码", "360.00", "张三", "示例机械加工有限公司"),
    ])
    pkg3 = export_bindle(api.entries, api.attachments, ids3, work_dir / "采购组报账.tidoc", profiles)
    for eid in ids3:
        backend.delete_entry(eid)
    # 写入一个额外文件触发完整性校验提醒，展示批量导入里「需核对」与高亮「单包预览」的状态。
    with zipfile.ZipFile(pkg3, "a") as zf:
        zf.writestr("extra.txt", "tampered")

    return [pkg1, pkg2, pkg3]


def seed_import_preview(work_dir: Path) -> list[Path]:
    """生成导入预览用的虚构文件：4 组 XML+PDF、1 个只有 PDF 的发票、1 张付款截图和 1 份查验单。

    发票号沿用 20 位虚构号码，PDF 正文里带同一个号码，扫描时能和 XML 配对。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    sellers = ["示例办公用品有限公司", "示例五金工具有限公司", "示例文具商行", "示例电子科技有限公司"]
    paths: list[Path] = []
    for offset, seller in enumerate(sellers):
        number = f"26999000000000000{301 + offset}"
        amount = f"{120 + offset * 35}.00"
        xml_path = work_dir / f"发票_{number}.xml"
        _invoice_xml(xml_path, number, "2026-09-24", seller, UNIVERSITY, "办公用品", amount)
        pdf_path = work_dir / f"发票_{number}.pdf"
        _text_pdf(pdf_path, ["E-INVOICE", f"Invoice No: {number}", f"Amount: {amount}"])
        paths.extend([pdf_path, xml_path])

    pdf_only = "26999000000000000305"
    pdf_only_path = work_dir / f"发票_{pdf_only}.pdf"
    _text_pdf(pdf_only_path, ["E-INVOICE", f"Invoice No: {pdf_only}", "Amount: 88.00"])
    paths.append(pdf_only_path)

    payment = work_dir / "付款截图_0924.png"
    Image.new("RGB", (240, 320), (232, 236, 243)).save(payment)
    paths.append(payment)

    inspection_number = "26999000000000000301"
    inspection = work_dir / f"发票查验结果_{inspection_number}.pdf"
    _text_pdf(inspection, ["VERIFICATION RESULT", f"Invoice No: {inspection_number}", "Check count: 1"])
    paths.append(inspection)
    return paths
