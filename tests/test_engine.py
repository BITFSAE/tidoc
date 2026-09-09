"""解析引擎与校验的回归测试（移植自 invoice2docx 的逻辑）。"""

from decimal import Decimal

import pytest

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
    set_title_profiles,
    supported_titles,
    title_profiles,
)
import tidoc.engine.parser as parser_module
from tidoc.engine.money import d
from tidoc.engine.parser import _parse_invoice_text, _parse_pdf_items, parse_pdf


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


def test_pdf_number_fallback_stays_on_one_line_and_keeps_thousands_separator():
    text = """电子发票（普通发票） 发票号码：
开票日期：2026年07月13日
价税合计（小写） ¥1,234.56
订单 1234567890
账号 9876543210
"""

    inv = _parse_invoice_text(text)

    assert inv.invoice_no == ""
    assert inv.total == Decimal("1234.56")


def test_pdf_number_fallback_rejects_more_than_twenty_spaced_digits():
    inv = _parse_invoice_text("编号：1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1")

    assert inv.invoice_no == ""


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


def test_pdf_layout_fallback_handles_user_supplied_messy_invoices(monkeypatch, tmp_path):
    cases = [
        (
            "发票号码：\n开票日期：\n价税合计（小写） ¥ 37.90\n"
            "2 6 4 2 2 0 0 0 0 0 2 174 96 974 6\n"
            "2 0 2 6 年 0 6 月 18日\n",
            "购  名称：太原理工大学                 销  名称：武汉启是科技有限公司\n"
            "统一社会信用代码/纳税人识别号：12140000405700021K"
            "                 统一社会信用代码/纳税人识别号：91420111MA4K4DRNX2\n"
            "*电子元件*MSPM0G3507核 不含线          个              1 37.5247524752475   37.52 1% 0.38\n"
            "心板\n",
            "26422000002174969746", "2026-06-18", "武汉启是科技有限公司", "太原理工大学",
            Decimal("37.90"), "MSPM0G3507核心板 不含线", "个", Decimal("1"),
        ),
        (
            "发票号码：26442000003444434596\n开票日期：2026年03月30日\n"
            "价税合计（小写） ¥ 17.59\n",
            "购  名称：太原理工大学                 销  名称：中山大简科技有限公司\n"
            "统一社会信用代码/纳税人识别号：12140000405700021K"
            "                 统一社会信用代码/纳税人识别号：914420003247671440\n"
            "*其他 化学制品*3D打印耗   PETG（无料盘）    公斤             115.5663716814159    15.57    13% 2.02\n"
            "材\n",
            "26442000003444434596", "2026-03-30", "中山大简科技有限公司", "太原理工大学",
            Decimal("17.59"), "3D打印耗材", "公斤", Decimal("1"),
        ),
        (
            "发票号码：26332000002742642046\n开票日期：2026年04月03日\n"
            "价税合计（小写） ¥69.00\n",
            "购  名称：太原理工大学                 台州市椒江西域电子厂销  名称：\n"
            "统一社会信用代码/纳税人识别号：12140000405700021K"
            "                 91331002L40170656B\n"
            "*敏感元件及传感器*角度                 支 168.316831683168368.32 1% 0.68\n"
            "位移传感器\n",
            "26332000002742642046", "2026-04-03", "台州市椒江西域电子厂", "太原理工大学",
            Decimal("69.00"), "角度位移传感器", "支", Decimal("1"),
        ),
    ]

    for index, (normal_text, layout_text, invoice_no, date, seller, buyer,
                total, item_name, unit, quantity) in enumerate(cases):
        path = tmp_path / f"invoice-{index}.pdf"
        monkeypatch.setattr(parser_module, "_pdf_text", lambda _path, value=normal_text: value)
        monkeypatch.setattr(parser_module, "_pdf_layout_text", lambda _path, value=layout_text: value)
        parsed = parse_pdf(path)
        assert (parsed.invoice_no, parsed.invoice_date) == (invoice_no, date)
        assert (parsed.seller, parsed.buyer_name) == (seller, buyer)
        assert parsed.total == total
        assert parsed.items[0].actual_name == item_name
        assert parsed.items[0].unit == unit
        assert parsed.items[0].quantity == quantity


def test_pdf_layout_removes_overlaid_headers_from_item_rows(monkeypatch, tmp_path):
    cases = [
        (
            "发票号码：26952000003669031936\n开票日期：2026年08月28日\n"
            "价税合计（小写） ¥215.00\n",
            "*有色金属压延材*铝板加项目名称 规格型号 单 位 数 量 单 价 "
            "212.87金 额税率/征收率1% 税 额2.13\n"
            "工定制\n",
            "铝板加工定制", "", None, Decimal("215.00"),
        ),
        (
            "发票号码：26922000000972420271\n开票日期：2026年08月19日\n"
            "价税合计（小写） ¥65.70\n",
            "*化学合成材料*结构胶项目名称 规格型号 单 位支 "
            "数 量165.049504950495单 价金 额65.05税率/征收率1% 税 额0.65\n",
            "结构胶", "支", Decimal("1"), Decimal("65.70"),
        ),
    ]

    for index, (normal_text, layout_text, name, unit, quantity, total) in enumerate(cases):
        path = tmp_path / f"overlaid-{index}.pdf"
        monkeypatch.setattr(parser_module, "_pdf_text", lambda _path, value=normal_text: value)
        monkeypatch.setattr(parser_module, "_pdf_layout_text", lambda _path, value=layout_text: value)

        parsed = parse_pdf(path)

        assert len(parsed.items) == 1
        assert parsed.items[0].actual_name == name
        assert parsed.items[0].unit == unit
        assert parsed.items[0].quantity == quantity
        assert parsed.items[0].total == total


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


def test_pdf_parses_columnar_single_item_text():
    text = """项目名称 规格型号 单位 数量 单价 金额 税率/征收率 税额
*橡胶制品*3M 防水密封胶带强力补漏空气蒸
汽隔离膜3015 40毫米宽*3米长一卷
3015-40
卷
2
37.17
74.34
9.66
13%
价税合计（小写）
¥84.00
"""

    inv = _parse_invoice_text(text)

    assert len(inv.items) == 1
    assert inv.items[0].actual_name == "3M防水密封胶带强力补漏空气蒸汽隔离膜301540毫米宽*3米长一卷"
    assert inv.items[0].unit == "卷"
    assert inv.items[0].quantity == Decimal("2")
    assert inv.items[0].total == Decimal("84.00")


def test_pdf_columnar_repeated_name_merges_discount_row():
    text = """*橡胶制品*防水密封胶带
*橡胶制品*防水密封胶带
3015-40
卷
4
37.1675
148.67
19.33
13%
-0.02
0.00
13%
价税合计（小写）
¥167.98
"""

    inv = _parse_invoice_text(text)

    assert len(inv.items) == 1
    assert inv.items[0].actual_name == "防水密封胶带"
    assert inv.items[0].total == Decimal("167.98")


def test_pdf_merges_rate_first_discount_row():
    text = """*衡器*电子秤 13%个 14.53 1.8914.53097345132741
*衡器*电子秤 13%-0.88 -0.12
价税合计（小写） ¥15.42
"""

    inv = _parse_invoice_text(text)

    assert len(inv.items) == 1
    assert inv.items[0].actual_name == "电子秤"
    assert inv.items[0].total == Decimal("15.42")


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


def test_pdf_layout_keeps_empty_buyer_tax_id_separate_from_seller(monkeypatch, tmp_path):
    normal_text = """电子发票（普通发票） 发票号码：26952000001957382236
开票日期：2026年05月13日
名称： 名称：
北京理工大学教育基金会 深圳维特智能科技有限公司
91440300359289517R
价税合计（小写） ¥855.00
"""
    layout_text = """购  名称：北京理工大学教育基金会      销  名称：深圳维特智能科技有限公司
购  统一社会信用代码/纳税人识别号：    销  统一社会信用代码/纳税人识别号：91440300359289517R
"""
    monkeypatch.setattr(parser_module, "_pdf_text", lambda _path: normal_text)
    monkeypatch.setattr(parser_module, "_pdf_layout_text", lambda _path: layout_text)

    parsed = parse_pdf(tmp_path / "empty-buyer-tax-id.pdf")

    assert parsed.buyer_name == "北京理工大学教育基金会"
    assert parsed.buyer_tax_id == ""
    assert parsed.seller == "深圳维特智能科技有限公司"


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


def test_layout_joined_numbers_support_multi_digit_quantity_and_amount():
    items = _parse_pdf_items(
        ["*电子元件*批量零件 个 1215.50186.00 13% 24.18"],
        layout=True,
    )

    assert len(items) == 1
    assert items[0].quantity == Decimal("12")
    assert items[0].total == Decimal("210.18")


def test_pdf_prefers_closure_parser_when_folded_regex_loses_unit_and_quantity():
    items = _parse_pdf_items([
        "*配电控制设备*接线端子6.3 包 2 4.950495049505 9.90 1% 0.10",
        "*金属制品*压线钳 SN-48B 把 134.653465346534734.65 1% 0.35",
    ])

    assert [(item.unit, item.quantity, item.total) for item in items] == [
        ("包", Decimal("2"), Decimal("10.00")),
        ("把", Decimal("1"), Decimal("35.00")),
    ]
    assert items[1].actual_name == "压线钳 SN-48B"


def test_layout_removes_model_letter_fused_into_chinese_unit():
    cases = [
        ("*电子元件*电源模块       URB2405YMD-10W个 2 12.21 24.42 13% 3.17", "个", Decimal("2")),
        ("*印制电路板*线路板       LV-PDM(Power_D片 5 50.984 254.92 13% 33.14", "片", Decimal("5")),
        ("*有色金属合金*定制壳体   CUS_7556653A_K个 1 39.62 39.62 13% 5.15", "个", Decimal("1")),
    ]

    for line, unit, quantity in cases:
        item = _parse_pdf_items([line], layout=True)[0]
        assert item.unit == unit
        assert item.quantity == quantity


def test_layout_does_not_treat_item_name_as_missing_unit():
    items = _parse_pdf_items([
        "*其他电子设备*价外费用                                  13.4653465346535      3.47    1%               0.03",
    ], layout=True)

    assert items[0].actual_name == "价外费用"
    assert items[0].unit == ""
    assert items[0].quantity == Decimal("1")
    assert items[0].total == Decimal("3.50")


def test_layout_preserves_missing_unit_and_quantity():
    items = _parse_pdf_items([
        "*有色金属压延材*铝板加工定制                         212.87 1% 2.13",
    ], layout=True)

    assert items[0].unit == ""
    assert items[0].quantity is None
    assert items[0].total == Decimal("215.00")


def test_layout_item_spec_continuation_does_not_extend_product_name():
    layout_lines = [
        "*电线电缆*测试线         双头注塑4mm香     条  1  20.099  20.10  1%  0.20",
        "                  蕉插头线",
    ]

    items = _parse_pdf_items(layout_lines, layout=True)

    assert items[0].actual_name == "测试线"


def test_layout_item_spec_containing_he_is_not_mistaken_for_total_row():
    layout_lines = [
        "*金属制品*螺丝刀       【S2钢更优】-25合1     套  1  43.36  43.36  13%  5.64",
        "                精密型（弹仓速取/",
        "                铝盒）",
        "       合       计                             ¥43.36       ¥5.64",
    ]

    items = _parse_pdf_items(layout_lines, layout=True)

    assert len(items) == 1
    assert items[0].actual_name == "螺丝刀"
    assert items[0].unit == "套"
    assert items[0].quantity == Decimal("1")
    assert items[0].total == Decimal("49.00")


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
        buyer_tax_id="12100000400009127B",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )
    assert check_invoice(inv).status == CHECK_PASS


def test_supported_title_without_buyer_tax_id_is_recognition_warning():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )

    result = check_invoice(inv)

    assert result.status == CHECK_WARNING
    assert "未能识别「北京理工大学」的购买方税号" in result.message
    assert "12100000400009127B" in result.message


def test_supported_title_with_wrong_buyer_tax_id_is_recognition_warning():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"),
        buyer_name="北京理工大学教育基金会",
        buyer_tax_id="12100000400009127B",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )

    result = check_invoice(inv)

    assert result.status == CHECK_WARNING
    assert "与「北京理工大学教育基金会」不一致" in result.message
    assert "53100000500021676K" in result.message
    assert "该税号属于「北京理工大学」" in result.message


def test_known_tax_id_reveals_unrecognized_buyer_title():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京某大学",
        buyer_tax_id="5310 0000-5000 2167 6k",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )

    result = check_invoice(inv)

    assert result.status == CHECK_WARNING
    assert "税号 53100000500021676K 属于「北京理工大学教育基金会」" in result.message


def test_item_sum_mismatch_is_non_blocking_recognition_warning():
    inv = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学",
        buyer_tax_id="12100000400009127B",
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
        buyer_tax_id="53100000500021676K",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )
    r = check_invoice(inv, expected_title="北京理工大学")
    assert r.status == CHECK_BLOCKED
    assert "不一致" in r.message


# ---------------- 可配置抬头与税号（多学校可用） ----------------

@pytest.fixture
def restore_title_profiles():
    """抬头配置是进程内全局状态，测试后必须恢复内置默认。"""
    yield
    set_title_profiles(None)


def _closed_invoice(buyer_name, buyer_tax_id=""):
    return ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )


def test_custom_school_title_passes_when_configured(restore_title_profiles):
    set_title_profiles([{"name": "清华大学", "tax_id": "1210000 4000-0999 99xa"}])

    assert check_invoice(
        _closed_invoice("清华大学", "1210000400009999 9XA")
    ).status == CHECK_PASS


def test_default_title_warns_after_switching_profiles(restore_title_profiles):
    set_title_profiles([{"name": "清华大学", "tax_id": ""}])

    result = check_invoice(_closed_invoice("北京理工大学", "12100000400009127B"))

    assert result.status == CHECK_WARNING
    assert "购买方抬头「北京理工大学」不在已配置的抬头内" in result.message


def test_title_without_tax_id_only_warns_on_name(restore_title_profiles):
    set_title_profiles([{"name": "清华大学", "tax_id": ""}])

    result = check_invoice(_closed_invoice("清华大学", "123456789012345678"))

    assert result.status == CHECK_PASS


def test_empty_title_profiles_skip_title_checks(restore_title_profiles):
    set_title_profiles([])

    result = check_invoice(_closed_invoice("任意单位"))

    assert result.status == CHECK_PASS
    assert "抬头" not in result.message


def test_reset_title_profiles_restores_builtin_defaults(restore_title_profiles):
    set_title_profiles([])
    assert supported_titles() == ()

    set_title_profiles(None)

    assert supported_titles() == ("北京理工大学", "北京理工大学教育基金会")
    assert title_profiles() == (
        ("北京理工大学", "12100000400009127B"),
        ("北京理工大学教育基金会", "53100000500021676K"),
    )


def test_set_title_profiles_dedupes_and_normalizes():
    profiles = set_title_profiles([
        {"name": " 复旦大学 ", "tax_id": "1210 0000-4000 0000 0a"},
        {"name": "复旦大学", "tax_id": "dup"},
        {"name": "", "tax_id": "x"},
    ])

    assert profiles == (("复旦大学", "12100000400000000A"),)
    set_title_profiles(None)


def test_parser_buyer_detection_uses_configured_titles(restore_title_profiles):
    set_title_profiles([{"name": "清华大学", "tax_id": ""}])

    assert parser_module._split_combined_party_names("清华大学资产公司") == [
        "清华大学", "资产公司",
    ]
