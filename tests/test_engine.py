"""解析引擎与校验的回归测试（移植自 invoice2docx 的逻辑）。"""

from decimal import Decimal

from tidoc.engine import (
    CHECK_BLOCKED,
    CHECK_PASS,
    CHECK_WARNING,
    ParsedInvoice,
    ParsedItem,
    check_invoice,
    clean_item_name,
    money,
    parse_xml,
)
from tidoc.engine.money import d
from tidoc.engine.parser import _parse_invoice_text, _parse_pdf_items


def test_money_helpers():
    assert d("1,234.50") == Decimal("1234.50")
    assert d("¥99.99") == Decimal("99.99")
    assert d("") == Decimal("0")
    assert d(None) == Decimal("0")
    assert money(Decimal("1.005")) == Decimal("1.01")


def test_clean_item_name():
    assert clean_item_name("*电子元件*电阻") == "电阻"
    assert clean_item_name("无星号名称") == "无星号名称"


def test_pdf_total_does_not_cross_newline_after_trailing_currency_sign():
    text = """电子发票（普通发票） 发票号码：26952000002955521026
开票日期：2026年07月13日
*电线电缆*测试线 双头注塑4mm香蕉插头线 条 1 20.0990099009901 20.10 1% 0.20
价税合计（小写） ¥20.30
20.10¥ 0.20¥
3301727052462005956
"""

    inv = _parse_invoice_text(text)

    assert inv.total == Decimal("20.30")
    assert inv.items[0].total == Decimal("20.30")


def test_pdf_parses_spaced_decimals_in_wrapped_item_line():
    text = """电子发票（普通发票） 发票号码：26952000001959929356
开票日期：2026年05月 13日
* 电子元件* 电阻器 200W 铝壳 1个
; 0. 5R
13%件 23. 01 2. 9923. 008 8 4 9557 52211
价税合计（小写） ¥ 26. 00
"""

    inv = _parse_invoice_text(text)

    assert inv.total == Decimal("26.00")
    assert inv.items[0].actual_name.startswith("电阻器")
    assert inv.items[0].total == Decimal("26.00")


def test_pdf_parses_wrapped_item_with_standard_numeric_tail():
    text = """电子发票（普通发票） 发票号码：26337000000651169782
开票日期：2026年07月06日
*计算机外部设备*绿联
typec拓展坞转USB3.2集线
器扩展10Gbps转换
CM639 件 1 106.74 106.74 13% 13.88
价税合计（小写） ¥ 120.62
"""

    inv = _parse_invoice_text(text)

    assert inv.items[0].actual_name.startswith("绿联typec拓展坞")
    assert inv.items[0].unit == "件"
    assert inv.items[0].quantity == Decimal("1")
    assert inv.items[0].total == Decimal("120.62")


def test_pdf_keeps_explicit_buyer_and_seller_roles_for_personal_invoice():
    text = """购买方信息
名称： 武理博
统一社会信用代码/纳税人识别号:
销售方信息
名称： 杭州洋橙电子商务有限公司
统一社会信用代码/纳税人识别号: 91330110MA7LQLWL32
电子发票（普通发票） 发票号码：26337000000651169782
开票日期：2026年07月06日
价税合计（小写） ¥ 120.62
"""

    inv = _parse_invoice_text(text)

    assert inv.buyer_name == "武理博"
    assert inv.buyer_tax_id == ""
    assert inv.seller == "杭州洋橙电子商务有限公司"


def test_pdf_skips_download_label_before_detached_party_values():
    text = """电子发票（普通发票） 发票号码：
开票日期：
购
买
方
信
息 统一社会信用代码/纳税人识别号：
销
售
方
信
息 统一社会信用代码/纳税人识别号：
名称： 名称：
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
合 计
价税合计（大写） （小写）
备
注
开票人：
下载次数：1
26332000000000000001
2026年07月21日
北京理工大学教育基金会
53100000500021676K
温州测试电子有限公司
9133030430755012X0
*金属制品*刮刀 把 1 13.72 13.72 13% 1.78
¥13.72 ¥1.78
壹拾伍圆伍角整 ¥15.50
测试开票人
"""

    inv = _parse_invoice_text(text)

    assert inv.buyer_name == "北京理工大学教育基金会"
    assert inv.buyer_tax_id == "53100000500021676K"
    assert inv.seller == "温州测试电子有限公司"


def test_layout_item_name_continuation_joins_name_column_and_merges_discount():
    layout_lines = [
        "*微电子组件*特殊功能放           AMC1311BDWVR    个  2  7.16  14.32  13%  1.86",
        "大器",
        "*微电子组件*特殊功能放                         -0.63  13%  -0.08",
        "大器",
    ]

    items = _parse_pdf_items(layout_lines, layout=True)

    assert len(items) == 1
    assert items[0].actual_name == "特殊功能放大器"
    assert items[0].total == Decimal("15.47")


def test_layout_item_spec_continuation_does_not_extend_product_name():
    layout_lines = [
        "*电线电缆*测试线         双头注塑4mm香     条  1  20.099  20.10  1%  0.20",
        "                  蕉插头线",
    ]

    items = _parse_pdf_items(layout_lines, layout=True)

    assert items[0].actual_name == "测试线"


def test_layout_item_name_can_continue_across_multiple_lines():
    layout_lines = [
        "*计算机配套产品*绿联      35265   个  1  61.858  61.86  13%  8.04",
        "usb无线网卡台式机wifi6",
        "接收发射器",
        "",
        "开票人：张天赐",
    ]

    items = _parse_pdf_items(layout_lines, layout=True)

    assert items[0].actual_name == "绿联usb无线网卡台式机wifi6接收发射器"


def test_parse_xml_fields(sample_xmls):
    inv = parse_xml(sample_xmls[0])
    assert inv.invoice_no
    assert inv.buyer_name
    assert inv.total > 0
    assert inv.items, "应识别出明细"
    assert inv.invoice_date  # IssueTime


def test_xml_amount_closure(sample_xmls):
    """所有 XML 样本：明细含税合计应等于价税合计（金额闭合）。"""
    checked = 0
    for path in sample_xmls:
        inv = parse_xml(path)
        if not inv.items:
            continue
        item_sum = sum((it.total for it in inv.items), Decimal("0"))
        assert money(inv.total - item_sum) == Decimal("0.00"), f"{path} 金额不闭合"
        checked += 1
    assert checked > 0


def test_check_pass():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )
    assert check_invoice(inv).status == CHECK_PASS


def test_item_sum_mismatch_is_non_blocking_recognition_warning():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("90.00"))],
    )
    r = check_invoice(inv)
    assert r.status == CHECK_WARNING
    assert "相差" in r.message
    assert "请以发票总额为准" in r.message


def test_check_title_isolation():
    """抬头与所属分区不一致时 blocked（第 7 节强隔离）。"""
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学教育基金会",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )
    r = check_invoice(inv, expected_title="北京理工大学")
    assert r.status == CHECK_BLOCKED
    assert "不一致" in r.message
