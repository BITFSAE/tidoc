"""发票解析的校验层：总额按标签读取、大写金额、合计闭合、两种读取方式的字段比对。

所有数据都是虚构的：公司、税号、金额都不对应任何真实发票。
"""

from decimal import Decimal

import pytest

import tidoc.engine.parser as parser_module
from tidoc.engine import (
    CHECK_PASS,
    CHECK_WARNING,
    ParsedInvoice,
    ParsedItem,
    RecognitionContext,
    check_invoice,
    parse_pdf,
    parse_xml,
)
from tidoc.engine.fields import (
    parse_chinese_amount,
    read_total_fields,
    select_total,
    tax_id_checksum_ok,
    uscc_check_char,
    words_match_total,
    is_valid_iso_date,
    is_valid_uscc,
)
from tidoc.engine.models import (
    FIELD_AGREE,
    FIELD_CONFLICT,
    FIELD_MISSING,
    FIELD_VERIFIED,
    ISSUE_ADVISORY,
    ISSUE_STRONG,
    TOTAL_SOURCE_LABEL,
    TOTAL_SOURCE_MAX_AMOUNT,
    TOTAL_SOURCE_PAIR,
)
from tidoc.engine.money import parse_amount
from tidoc.engine.parser import _parse_invoice_text, _parse_loose_amount_tax_line


def make_uscc(prefix17: str) -> str:
    """给 17 位前缀补上合法的统一社会信用代码校验码（虚构代码用）。"""
    return prefix17 + uscc_check_char(prefix17)


BUYER_NAME = "示例大学"
SELLER_NAME = "示例设备有限公司"
OTHER_SELLER = "样品电子有限公司"
BUYER_ID = make_uscc("9111000071000001A")
SELLER_ID = make_uscc("91110000MA00ABCDE")
OTHER_ID = make_uscc("91310000MA1FQ8R2K")
INVOICE_NO = "26110000000000000001"
OTHER_INVOICE_NO = "26110000000000000002"
CONTEXT = RecognitionContext(({"name": BUYER_NAME, "tax_id": BUYER_ID},))


def codes(result) -> set:
    return {issue.code for issue in result.issues}


def levels(result) -> dict:
    return {issue.code: issue.level for issue in result.issues}


# --------------------------------------------------------------------------- 文本样本

def layout_text(
    *, total="126.00", words="壹佰贰拾陆圆整", subtotal="111.50", tax="14.50",
    invoice_no=INVOICE_NO, date="2026年03月05日", seller=SELLER_NAME, buyer=BUYER_NAME,
    seller_id=SELLER_ID, buyer_id=BUYER_ID, extra="",
) -> str:
    """版面文本：购销双方并排，合计行与价税合计各占一行。"""
    return (
        f"电子发票（普通发票）                    发票号码：{invoice_no}\n"
        f"                                      开票日期：{date}\n"
        f"购  名称：{buyer}                    销  名称：{seller}\n"
        f"买  统一社会信用代码/纳税人识别号：{buyer_id}      售  统一社会信用代码/纳税人识别号：{seller_id}\n"
        "*电子元件*示例电阻      个  10  11.15  111.50  13%  14.50\n"
        f"  合       计                             ¥{subtotal}       ¥{tax}\n"
        f"价税合计（大写） {words} （小写）¥{total}\n"
        f"{extra}"
    )


def plain_text(
    *, total="126.00", words="壹佰贰拾陆圆整", subtotal="111.50", tax="14.50",
    invoice_no=INVOICE_NO, date="2026年03月05日", seller=SELLER_NAME, buyer=BUYER_NAME,
    seller_id=SELLER_ID, buyer_id=BUYER_ID, extra="",
) -> str:
    """普通文本流：标签与取值被拆开，只剩「大写 + 小写」同行。"""
    return (
        f"电子发票（普通发票） 发票号码：{invoice_no}\n"
        f"开票日期：{date}\n"
        f"名称：{buyer}\n"
        f"统一社会信用代码/纳税人识别号：{buyer_id}\n"
        f"名称：{seller}\n"
        f"统一社会信用代码/纳税人识别号：{seller_id}\n"
        "价税合计（大写） （小写）\n"
        "*电子元件*示例电阻 个 10 11.15 111.50 13% 14.50\n"
        f"¥ {subtotal} ¥ {tax}\n"
        f"{words} ¥ {total}\n"
        f"{extra}"
    )


@pytest.fixture
def parse_texts(monkeypatch, tmp_path):
    """用给定的两份文本走一遍完整的 PDF 解析（普通文本 + 版面文本）。"""

    def run(plain, layout, context=CONTEXT):
        monkeypatch.setattr(parser_module, "_pdf_text", lambda _path: plain)
        monkeypatch.setattr(parser_module, "_pdf_layout_text", lambda _path: layout)
        return parse_pdf(tmp_path / "fictional.pdf", context=context)

    return run


# --------------------------------------------------------------------------- 金额转换

def test_parse_amount_separates_missing_unreadable_and_zero():
    assert parse_amount(None) is None
    assert parse_amount("") is None
    assert parse_amount("  ") is None
    assert parse_amount("abc") is None
    assert parse_amount(",") is None
    assert parse_amount("NaN") is None
    assert parse_amount("0") == Decimal("0")
    assert parse_amount("0.00") == Decimal("0.00")
    assert parse_amount("¥ 1,234.50") == Decimal("1234.50")
    assert parse_amount("-12.5") == Decimal("-12.5")


def test_loose_tail_without_any_amount_is_not_read_as_zero():
    # 行尾只剩「税额 + 税率」粘在一起，前面没有任何金额：不能凭空补一个 0 元的金额。
    assert _parse_loose_amount_tax_line("公牛 3.2113%") is None
    unit, quantity, amount, tax = _parse_loose_amount_tax_line("个 1 94.69 94.69 12.3113%")
    assert (amount, tax) == (Decimal("94.69"), Decimal("12.31"))


# --------------------------------------------------------------------------- 大写金额

@pytest.mark.parametrize(
    "text, expected",
    [
        ("壹佰贰拾陆元整", "126.00"),
        ("壹佰贰拾陆圆整", "126.00"),
        ("叁拾圆叁角陆分", "30.36"),
        ("壹万零伍佰圆整", "10500.00"),
        ("壹仟万元整", "10000000.00"),
        ("壹亿贰仟万元整", "120000000.00"),
        ("零圆伍角", "0.50"),
        ("叁角伍分", "0.35"),
        ("壹圆零伍分", "1.05"),
        ("拾伍圆整", "15.00"),
        ("壹拾元整", "10.00"),
        ("零元整", "0.00"),
        ("壹佰零叁圆玖角伍分", "103.95"),
        ("贰拾叁圆玖角整", "23.90"),
        ("⊗壹佰元整", "100.00"),
        ("人民币壹佰元整", "100.00"),
        ("壹 佰 元 整", "100.00"),
        ("负数壹佰元整", "-100.00"),
        ("（负数）壹佰元整", "-100.00"),
        ("负壹佰元整", "-100.00"),
        ("(负数)叁拾圆叁角陆分", "-30.36"),
    ],
)
def test_chinese_amount_is_converted_to_decimal(text, expected):
    assert parse_chinese_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    ["", None, "元整", "壹佰", "壹零元整", "壹佰元伍", "壹佰元分", "abc", "壹佰元整元", "壹壹元整", "万元", "分角"],
)
def test_malformed_chinese_amount_is_rejected(text):
    assert parse_chinese_amount(text) is None


def test_words_match_total_compares_sign_only_when_words_carry_one():
    assert words_match_total(Decimal("126.00"), Decimal("126.00"))
    assert not words_match_total(Decimal("127.00"), Decimal("126.00"))
    # 红字发票：大写写了「负」就按符号比较；没写负号时只比数额，避免误报。
    assert words_match_total(Decimal("-100.00"), Decimal("-100.00"))
    assert words_match_total(Decimal("100.00"), Decimal("-100.00"))
    assert not words_match_total(Decimal("-100.00"), Decimal("100.00"))


# --------------------------------------------------------------------------- 税号与日期

def test_uscc_check_digit_follows_gb_32100():
    assert is_valid_uscc(BUYER_ID) and is_valid_uscc(SELLER_ID) and is_valid_uscc(OTHER_ID)
    assert uscc_check_char("91110000MA00ABCDE") == SELLER_ID[-1]
    # 改动任意一位，校验位都对不上（这几个位置的权重与 31 互质）。
    for index in (0, 5, 9, 16):
        broken = SELLER_ID[:index] + ("2" if SELLER_ID[index] != "2" else "3") + SELLER_ID[index + 1:]
        assert not is_valid_uscc(broken)
    # 字符集不含 I O S V Z。
    assert uscc_check_char("9111000071000001O") is None
    assert not is_valid_uscc("9111000071000001OA")


def test_repository_builtin_title_tax_ids_pass_the_check_digit():
    from tidoc.engine.validator import TAX_ID_FOUNDATION, TAX_ID_UNIVERSITY

    assert is_valid_uscc(TAX_ID_UNIVERSITY)
    assert is_valid_uscc(TAX_ID_FOUNDATION)


def test_tax_id_checksum_only_applies_to_suspected_credit_codes():
    assert tax_id_checksum_ok(SELLER_ID) is True
    assert tax_id_checksum_ok(SELLER_ID.lower().replace("9", "9 ", 1)) is True  # 空白与大小写不影响
    broken = SELLER_ID[:-1] + ("0" if SELLER_ID[-1] != "0" else "1")
    assert tax_id_checksum_ok(broken) is False
    assert tax_id_checksum_ok("") is None
    assert tax_id_checksum_ok("91330110MA7LQLW") is None              # 15 位旧税号
    assert tax_id_checksum_ok("123456789012") is None                 # 非 18 位
    assert tax_id_checksum_ok("wrong") is None


def test_personal_id_card_numbers_are_not_checked_as_credit_codes():
    # 虚构的合法身份证号：出生日期 1990-01-01，校验位按 GB 11643 计算。
    prefix = "11010519900101123"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check = "10X98765432"[sum(int(c) * w for c, w in zip(prefix, weights)) % 11]
    assert tax_id_checksum_ok(prefix + check) is None


def test_date_must_exist_on_the_calendar():
    assert is_valid_iso_date("2026-03-05")
    assert is_valid_iso_date("2028-02-29")
    assert not is_valid_iso_date("2026-02-29")
    assert not is_valid_iso_date("2026-13-05")
    assert not is_valid_iso_date("2026-00-10")
    assert not is_valid_iso_date("2026-3-5")
    assert not is_valid_iso_date("")


# --------------------------------------------------------------------------- 总额：标签优先，最大金额兜底

def test_total_comes_from_the_label_even_when_a_larger_amount_is_on_the_page():
    # 备注里的订单金额比价税合计大：以前取 max(¥金额) 会把它当成总额。
    text = layout_text(extra="备注：订单金额 ¥1000.00\n")

    invoice = _parse_invoice_text(text, source="pdf-layout")

    assert invoice.total == Decimal("126.00")
    assert invoice.total_source == TOTAL_SOURCE_LABEL
    assert invoice.subtotal == Decimal("111.50") and invoice.tax_total == Decimal("14.50")
    assert invoice.total_in_words == Decimal("126.00")
    assert check_invoice(invoice, context={"scheme": {"titles": []}}).issues == []


def test_total_is_read_from_large_and_small_amount_on_one_line_without_label():
    invoice = _parse_invoice_text(plain_text(), context=CONTEXT)

    assert invoice.total == Decimal("126.00")
    assert invoice.total_source == TOTAL_SOURCE_PAIR
    assert (invoice.subtotal, invoice.tax_total) == (Decimal("111.50"), Decimal("14.50"))


def test_total_fields_accept_spaced_and_bracket_variants():
    text = (
        "合 计 ¥ 111. 50 ¥ 14. 50\n"
        "价税合计 （ 大写 ） ⊗壹佰贰拾陆圆整 （ 小写 ） ¥ 126. 00\n"
    )

    found = read_total_fields(text)

    assert found.total == Decimal("126.00")
    assert found.words == Decimal("126.00")
    assert (found.subtotal, found.tax_total) == (Decimal("111.50"), Decimal("14.50"))


def test_total_is_derived_when_words_stand_alone_and_match_a_printed_amount():
    text = "价税合计（大写） （小写）\n壹佰贰拾陆圆整\n¥ 126.00\n¥ 14.50\n"

    total, source = select_total(read_total_fields(text))

    assert (total, source) == (Decimal("126.00"), "derived")
    # 大写金额在页面上找不到对应的 ¥ 金额时不能当作总额，退回低可信的兜底。
    total, source = select_total(read_total_fields("价税合计（大写） （小写）\n壹佰贰拾陆圆整\n¥ 99.00\n"))
    assert (total, source) == (Decimal("99.00"), TOTAL_SOURCE_MAX_AMOUNT)


def test_largest_amount_is_only_a_flagged_last_resort():
    text = (
        "电子发票（普通发票） 发票号码：" + INVOICE_NO + "\n"
        "*电子元件*示例电阻 个 10 11.15 111.50 13% 14.50\n"
        "¥111.50\n¥14.50\n¥126.00\n"
    )

    invoice = _parse_invoice_text(text)
    result = check_invoice(invoice, context={"scheme": {"titles": []}})

    assert invoice.total == Decimal("126.00")
    assert invoice.total_source == TOTAL_SOURCE_MAX_AMOUNT
    assert invoice.total_present is True
    assert "total_low_confidence" in codes(result)
    assert levels(result)["total_low_confidence"] == ISSUE_STRONG
    assert result.status == CHECK_WARNING
    assert "可信度低" in result.message


def test_low_confidence_flag_does_not_apply_once_the_total_was_overridden():
    # 重新识别时条目里人工确认过的总额会覆盖解析结果；此时不应再说「暂用最大金额」。
    invoice = _parse_invoice_text("¥111.50\n¥14.50\n¥126.00\n")
    invoice.total = Decimal("125.00")
    invoice.items = [ParsedItem("*a*甲", "甲", "个", Decimal("1"), Decimal("125.00"))]

    assert "total_low_confidence" not in codes(check_invoice(invoice, context={"scheme": {"titles": []}}))


def test_no_amount_at_all_is_reported_as_missing_not_as_zero():
    invoice = _parse_invoice_text("电子发票（普通发票） 发票号码：" + INVOICE_NO + "\n")
    invoice.items = [ParsedItem("*a*甲", "甲", "个", Decimal("1"), Decimal("126.00"))]

    result = check_invoice(invoice, context={"scheme": {"titles": []}})

    assert invoice.total_present is False
    assert "total_missing" in codes(result)
    assert "未能识别发票总额" in result.message
    # 占位的 0 不能再拿去和明细比出一个「相差 126.00」。
    assert "相差" not in result.message


def test_garbled_currency_amount_is_dropped_instead_of_becoming_zero():
    # ¥ 后面只剩一个逗号：以前会被读成 0 元并参与取最大值。
    invoice = _parse_invoice_text("价税合计（小写） ¥ ,\n")

    assert invoice.total_present is False


# --------------------------------------------------------------------------- 合计闭合、大写金额、税率

def test_consistent_invoice_has_no_reminders():
    invoice = _parse_invoice_text(layout_text(), source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert result.issues == []
    assert "价税合计" not in result.message


def test_subtotal_plus_tax_must_equal_total_to_the_cent():
    invoice = _parse_invoice_text(layout_text(tax="14.49"), source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert "total_not_closed" in codes(result)
    assert levels(result)["total_not_closed"] == ISSUE_STRONG
    assert "不含税金额合计 111.50 加税额合计 14.49 为 125.99，与价税合计 126.00 不一致" in result.message
    # 与「抬头与分区冲突」不同，金额不闭合只是识别提醒，不阻断后续整理和导出。
    assert result.status == CHECK_WARNING


def test_words_that_disagree_with_the_small_amount_are_reported():
    invoice = _parse_invoice_text(layout_text(words="壹佰贰拾柒圆整"), source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert codes(result) == {"words_mismatch"}
    assert "大写金额「壹佰贰拾柒圆整」折合 127.00，与价税合计 126.00 不一致" in result.message


def test_words_with_jiao_and_fen_are_compared_to_the_cent():
    ok = _parse_invoice_text(
        layout_text(total="30.36", words="叁拾圆叁角陆分", subtotal="26.87", tax="3.49"),
        source="pdf-layout", context=CONTEXT,
    )
    bad = _parse_invoice_text(
        layout_text(total="30.36", words="叁拾圆叁角伍分", subtotal="26.87", tax="3.49"),
        source="pdf-layout", context=CONTEXT,
    )

    assert check_invoice(ok, context=CONTEXT).issues == []
    assert codes(check_invoice(bad, context=CONTEXT)) == {"words_mismatch"}


def test_unreadable_words_are_an_advisory_not_a_mismatch():
    invoice = _parse_invoice_text(layout_text(words="壹佰壹佰圆整"), source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert codes(result) == {"words_unreadable"}
    assert levels(result)["words_unreadable"] == ISSUE_ADVISORY


def test_red_invoice_with_negative_amounts_is_not_flagged():
    text = layout_text(
        total="-126.00", words="（负数）壹佰贰拾陆圆整", subtotal="-111.50", tax="-14.50",
    ).replace("111.50  13%  14.50", "-111.50  13%  -14.50")
    invoice = _parse_invoice_text(text, source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.total == Decimal("-126.00")
    assert invoice.total_in_words == Decimal("-126.00")
    assert result.issues == []
    assert not any(word in result.message for word in ("价税合计", "大写金额", "不含税金额合计"))


def test_red_invoice_whose_words_omit_the_sign_is_not_flagged():
    text = layout_text(total="-126.00", words="壹佰贰拾陆圆整", subtotal="-111.50", tax="-14.50")
    invoice = _parse_invoice_text(text, source="pdf-layout", context=CONTEXT)

    assert check_invoice(invoice, context=CONTEXT).issues == []


def test_red_invoice_fallback_picks_the_amount_with_the_largest_magnitude():
    # max() 在全是负数时会挑出绝对值最小的税额；兜底要按绝对值取。
    invoice = _parse_invoice_text("¥-111.50\n¥-14.50\n¥-126.00\n")

    assert invoice.total == Decimal("-126.00")
    assert invoice.total_source == TOTAL_SOURCE_MAX_AMOUNT


def test_total_may_legitimately_be_zero():
    text = layout_text(total="0.00", words="零圆整", subtotal="0.00", tax="0.00")
    invoice = _parse_invoice_text(text, source="pdf-layout", context=CONTEXT)

    assert invoice.total_present is True
    assert invoice.total == Decimal("0.00")
    assert check_invoice(invoice, context=CONTEXT).issues == []


def test_discount_line_keeps_the_totals_closed():
    # 折扣行把不含税合计和税额合计一起拉低；合计行按折后数填写，仍然闭合。
    text = (
        "*电子元件*示例电阻      个  10  11.15  111.50  13%  14.50\n"
        "*电子元件*示例电阻                        -11.15  13%  -1.45\n"
        "  合       计                             ¥100.35       ¥13.05\n"
        "价税合计（大写） 壹佰壹拾叁圆肆角整 （小写）¥113.40\n"
    )

    invoice = _parse_invoice_text(text, source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.total == Decimal("113.40")
    assert sum(item.total for item in invoice.items) == Decimal("113.40")
    assert result.issues == []


def test_tax_rate_estimate_is_only_an_advisory():
    # 税额明显不是 13% 也不是任何票面税率：只给辅助级提示，且合计仍然闭合所以没有强提醒。
    text = layout_text(total="111.50", words="壹佰壹拾壹圆伍角整", subtotal="100.00", tax="11.50")
    text = text.replace("14.50", "11.50")
    off = _parse_invoice_text(text.replace("13%", "6%"), source="pdf-layout", context=CONTEXT)
    result = check_invoice(off, context=CONTEXT)

    assert "tax_rate_estimate" in codes(result)
    assert levels(result)["tax_rate_estimate"] == ISSUE_ADVISORY
    assert not [issue for issue in result.issues if issue.level == ISSUE_STRONG]
    assert result.status == CHECK_WARNING


def test_tax_rate_estimate_tolerates_rounding_mixed_rates_and_exemption():
    base = dict(source="pdf-layout", context=CONTEXT)
    # 逐行舍入：3 行各 1% 税额，合计与 1% 相差 0.01。
    rounded = _parse_invoice_text(
        layout_text(total="303.02", words="叁佰零叁圆零贰分", subtotal="300.00", tax="3.02").replace("13%", "1%"),
        **base,
    )
    assert "tax_rate_estimate" not in codes(check_invoice(rounded, context=CONTEXT))
    # 多税率：13% 与 6% 混合，整体比例落在两者之间。
    mixed = _parse_invoice_text(
        layout_text(total="219.00", words="贰佰壹拾玖圆整", subtotal="200.00", tax="19.00")
        + "*服务*示例服务 项 1 50.00 50.00 6% 3.00\n",
        **base,
    )
    assert "tax_rate_estimate" not in codes(check_invoice(mixed, context=CONTEXT))
    # 免税：税额为 0 是合法的。
    exempt = _parse_invoice_text(
        layout_text(total="100.00", words="壹佰圆整", subtotal="100.00", tax="0.00").replace("13%", "免税"),
        **base,
    )
    assert exempt.tax_rates == [Decimal("0")]
    assert check_invoice(exempt, context=CONTEXT).issues == []


def test_invoices_built_without_parser_facts_get_no_new_reminders():
    invoice = ParsedInvoice(
        invoice_no="1", total=Decimal("100.00"), buyer_name="北京理工大学",
        buyer_tax_id="12100000400009127B",
        items=[ParsedItem("*x*甲", "甲", "个", Decimal("1"), Decimal("100.00"))],
    )

    result = check_invoice(invoice)

    assert result.status == CHECK_PASS
    assert result.issues == []


def test_new_reminders_never_contain_the_message_separator():
    # check_message 用「；」连接多条提醒，再按它拆开；票面内容里的「；」必须先换掉。
    invoice = _parse_invoice_text(layout_text(words="壹佰；贰拾柒圆整"), source="pdf-layout", context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert all("；" not in issue.message for issue in result.issues)
    assert len(result.message.split("；")) == len([m for m in result.message.split("；") if m])


# --------------------------------------------------------------------------- 两种读取方式逐字段比对

def test_agreeing_paths_record_agreement_and_no_reminders(parse_texts):
    invoice = parse_texts(plain_text(), layout_text())
    result = check_invoice(invoice, context=CONTEXT)

    checks = invoice.field_checks
    assert checks["invoice_no"].state == FIELD_AGREE
    assert checks["invoice_date"].state == FIELD_AGREE
    assert checks["seller"].state == FIELD_AGREE and checks["buyer_name"].state == FIELD_AGREE
    assert checks["seller_tax_id"].state == FIELD_AGREE and checks["buyer_tax_id"].state == FIELD_AGREE
    assert checks["total"].state == FIELD_VERIFIED and checks["total"].basis == "words+closure"
    assert invoice.seller_tax_id == SELLER_ID
    assert invoice.buyer_tax_id == BUYER_ID
    assert invoice.total == Decimal("126.00")
    assert result.issues == []


def test_invoice_number_located_by_its_label_beats_a_bare_twenty_digit_run(parse_texts):
    # 普通文本里 20 位数字出现在别处（例如校验码），版面文本按「发票号码」标签定位。
    plain = plain_text().replace(f"发票号码：{INVOICE_NO}", "发票号码：").replace(
        "开票日期：", f"校验码 {OTHER_INVOICE_NO}\n开票日期：", 1,
    )
    invoice = parse_texts(plain, layout_text())

    assert invoice.invoice_no == INVOICE_NO
    assert invoice.field_checks["invoice_no"].state == FIELD_VERIFIED
    assert invoice.field_checks["invoice_no"].basis == "label"
    assert "invoice_no_conflict" not in codes(check_invoice(invoice, context=CONTEXT))


def test_two_different_labelled_invoice_numbers_are_a_conflict_not_a_silent_choice(parse_texts):
    invoice = parse_texts(plain_text(), layout_text(invoice_no=OTHER_INVOICE_NO))
    result = check_invoice(invoice, context=CONTEXT)

    check = invoice.field_checks["invoice_no"]
    assert check.state == FIELD_CONFLICT
    assert check.candidates == {"text": INVOICE_NO, "layout": OTHER_INVOICE_NO}
    assert invoice.invoice_no == INVOICE_NO            # 沿用原来的取值：普通文本
    assert "invoice_no_conflict" in codes(result)
    assert f"发票号码识别到两个不同的结果：「{INVOICE_NO}」与「{OTHER_INVOICE_NO}」" in result.message
    assert result.status == CHECK_WARNING


def test_missing_invoice_number_in_both_paths_is_recorded_and_reminded(parse_texts):
    plain = plain_text().replace(f"发票号码：{INVOICE_NO}", "发票号码：")
    layout = layout_text().replace(f"发票号码：{INVOICE_NO}", "发票号码：")
    invoice = parse_texts(plain, layout)
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.invoice_no == ""
    assert invoice.field_checks["invoice_no"].state == FIELD_MISSING
    assert "invoice_no_missing" in codes(result)
    assert "未能识别发票号码" in result.message


def test_one_sided_values_are_accepted_without_a_reminder(parse_texts):
    # 普通文本没读到日期，版面文本读到了：采用它，记为「缺失」（无法比对），不打扰用户。
    plain = plain_text().replace("开票日期：2026年03月05日", "开票日期：")
    invoice = parse_texts(plain, layout_text())

    assert invoice.invoice_date == "2026-03-05"
    assert invoice.field_checks["invoice_date"].state == FIELD_MISSING
    assert invoice.field_checks["invoice_date"].candidates == {"text": "", "layout": "2026-03-05"}
    assert check_invoice(invoice, context=CONTEXT).issues == []


def test_impossible_date_loses_to_a_real_date_and_a_lone_one_is_flagged(parse_texts):
    both = parse_texts(plain_text(date="2026年13月45日"), layout_text())
    assert both.invoice_date == "2026-03-05"
    assert both.field_checks["invoice_date"].state == FIELD_VERIFIED
    assert both.field_checks["invoice_date"].basis == "valid_date"
    assert check_invoice(both, context=CONTEXT).issues == []

    lone = parse_texts(plain_text(date="2026年02月30日"), layout_text(date="2026年02月30日"))
    result = check_invoice(lone, context=CONTEXT)
    assert lone.invoice_date == "2026-02-30"
    assert "invoice_date_invalid" in codes(result)
    assert levels(result)["invoice_date_invalid"] == ISSUE_ADVISORY
    assert "开票日期「2026-02-30」不是有效日期" in result.message


def test_seller_conflict_is_recorded_and_the_layout_value_is_still_used(parse_texts):
    invoice = parse_texts(plain_text(seller=OTHER_SELLER), layout_text())
    result = check_invoice(invoice, context=CONTEXT)

    check = invoice.field_checks["seller"]
    assert check.state == FIELD_CONFLICT
    assert check.candidates == {"text": OTHER_SELLER, "layout": SELLER_NAME}
    assert invoice.seller == SELLER_NAME               # 版面文本带「购 / 销」标签，沿用原来的取值
    assert "seller_conflict" in codes(result)
    assert f"销售方名称识别到两个不同的结果：「{OTHER_SELLER}」与「{SELLER_NAME}」，已采用「{SELLER_NAME}」" in result.message


def test_buyer_name_conflict_is_settled_by_a_configured_title(parse_texts):
    wrong = "示例学院"
    invoice = parse_texts(plain_text(buyer=wrong), layout_text())
    check = invoice.field_checks["buyer_name"]

    # 示例大学是已配置的抬头，用它独立判定；版面文本的读数被采用且不再提醒。
    assert (check.state, check.basis) == (FIELD_VERIFIED, "configured_title")
    assert invoice.buyer_name == BUYER_NAME
    assert "buyer_name_conflict" not in codes(check_invoice(invoice, context=CONTEXT))

    # 反过来，普通文本读到已配置的抬头而版面文本不是：不改用普通文本，按冲突提醒。
    reverse = parse_texts(plain_text(buyer=BUYER_NAME), layout_text(buyer="示例学院"))
    assert reverse.buyer_name == "示例学院"
    assert reverse.field_checks["buyer_name"].state == FIELD_CONFLICT
    assert "buyer_name_conflict" in codes(check_invoice(reverse, context=CONTEXT))

    # 没有配置任何抬头时无从判定，保持冲突并提醒。
    unconfigured = parse_texts(plain_text(buyer=wrong), layout_text(), context=RecognitionContext(()))
    assert unconfigured.field_checks["buyer_name"].state == FIELD_CONFLICT


def test_tax_id_conflict_is_a_reminder_and_empty_layout_cell_is_not(parse_texts):
    conflict = parse_texts(plain_text(seller_id=OTHER_ID), layout_text())
    assert conflict.field_checks["seller_tax_id"].state == FIELD_CONFLICT
    assert conflict.seller_tax_id == SELLER_ID
    assert "seller_tax_id_conflict" in codes(check_invoice(conflict, context=CONTEXT))

    # 版面文本里购买方税号格子是空的（个人抬头）：沿用空值，不去借普通文本里可能属于对方的税号。
    plain = plain_text(buyer_id=SELLER_ID)
    layout = layout_text(buyer_id="")
    empty = parse_texts(plain, layout)
    assert empty.buyer_tax_id == ""
    assert empty.field_checks["buyer_tax_id"].state == FIELD_MISSING
    assert empty.field_checks["buyer_tax_id"].basis == "layout_cell_empty"
    assert "buyer_tax_id_conflict" not in codes(check_invoice(empty, context=CONTEXT))


def test_credit_code_with_a_wrong_check_digit_gets_an_advisory(parse_texts):
    bad_id = SELLER_ID[:-1] + ("0" if SELLER_ID[-1] != "0" else "1")
    invoice = parse_texts(plain_text(seller_id=bad_id), layout_text(seller_id=bad_id))
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.seller_tax_id == bad_id
    assert "seller_tax_id_checksum" in codes(result)
    assert levels(result)["seller_tax_id_checksum"] == ISSUE_ADVISORY
    assert f"销售方税号「{bad_id}」的校验位不符" in result.message
    # 个人抬头、旧 15 位税号不报错。
    assert "buyer_tax_id_checksum" not in codes(result)
    short = parse_texts(plain_text(seller_id="91330110MA7LQLW"), layout_text(seller_id="91330110MA7LQLW"))
    assert "seller_tax_id_checksum" not in codes(check_invoice(short, context=CONTEXT))


def test_roles_swapped_in_plain_text_are_confirmed_by_the_layout_anchors(parse_texts):
    # 没有已配置的抬头可参照时，普通文本流按出现顺序把先出现的当作销售方，恰好读反；
    # 版面文本的「购 / 销」标签是独立依据，不当作冲突。
    invoice = parse_texts(plain_text(), layout_text(), context=RecognitionContext(()))
    result = check_invoice(invoice, context=RecognitionContext(()))

    assert invoice.seller == SELLER_NAME and invoice.buyer_name == BUYER_NAME
    assert invoice.seller_tax_id == SELLER_ID and invoice.buyer_tax_id == BUYER_ID
    for name in ("seller", "buyer_name", "seller_tax_id", "buyer_tax_id"):
        assert invoice.field_checks[name].state == FIELD_VERIFIED
        assert invoice.field_checks[name].basis == "role_anchor"
    assert result.issues == []


def test_tax_stamp_text_picked_up_by_plain_text_is_not_a_party_name(parse_texts):
    plain = plain_text(seller="国家税务总局全", buyer="国统一")
    invoice = parse_texts(plain, layout_text(), context=RecognitionContext(()))

    assert invoice.seller == SELLER_NAME and invoice.buyer_name == BUYER_NAME
    assert invoice.field_checks["seller"].state == FIELD_MISSING
    assert "seller_conflict" not in codes(check_invoice(invoice, context=RecognitionContext(())))


def test_total_conflict_between_paths_is_settled_by_the_words_when_they_confirm_one(parse_texts):
    # 普通文本里小写总额读成 126.50；大写金额和合计行都支持版面文本的 126.00。
    invoice = parse_texts(plain_text(total="126.50"), layout_text())

    check = invoice.field_checks["total"]
    assert (check.state, check.basis) == (FIELD_VERIFIED, "words+closure")
    assert check.candidates == {"text": "126.50", "layout": "126.00"}
    assert invoice.total == Decimal("126.00")
    assert check_invoice(invoice, context=CONTEXT).issues == []


def test_total_conflict_between_paths_without_independent_evidence_is_reported(parse_texts):
    # 没有大写金额，也没有合计行：两条路径各按标签读到一个总额，无从判断谁对。
    plain = f"电子发票（普通发票） 发票号码：{INVOICE_NO}\n价税合计（小写） ¥126.50\n"
    layout = f"电子发票（普通发票）  发票号码：{INVOICE_NO}\n价税合计（小写）¥126.00\n"
    invoice = parse_texts(plain, layout)
    result = check_invoice(invoice, context=CONTEXT)

    check = invoice.field_checks["total"]
    assert check.state == FIELD_CONFLICT
    assert invoice.total == Decimal("126.50")            # 沿用原来的取值：普通文本
    assert "total_conflict" in codes(result)
    assert "价税合计识别到两个不同的结果：「126.50」与「126.00」，已采用「126.50」" in result.message


def test_total_found_by_only_one_path_is_not_a_conflict(parse_texts):
    plain = plain_text().replace("壹佰贰拾陆圆整 ¥ 126.00\n", "").replace("¥ 111.50 ¥ 14.50\n", "")
    invoice = parse_texts(plain, layout_text())

    assert invoice.total == Decimal("126.00")
    assert invoice.field_checks["total"].state == FIELD_VERIFIED
    assert "total_conflict" not in codes(check_invoice(invoice, context=CONTEXT))


def test_layout_extraction_failure_keeps_the_plain_result(monkeypatch, tmp_path):
    def boom(_path):
        raise RuntimeError("layout mode unsupported")

    monkeypatch.setattr(parser_module, "_pdf_text", lambda _path: plain_text())
    monkeypatch.setattr(parser_module, "_pdf_layout_text", boom)

    invoice = parse_pdf(tmp_path / "x.pdf", context=CONTEXT)

    assert invoice.invoice_no == INVOICE_NO
    assert invoice.total == Decimal("126.00")
    assert invoice.field_checks["invoice_no"].state == FIELD_AGREE   # 只有一条路径，没有可比较的对象
    assert check_invoice(invoice, context=CONTEXT).issues == []


# --------------------------------------------------------------------------- 官方电子发票 XML

XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<EInvoice>
  <Header><EIid>{invoice_no}</EIid></Header>
  <EInvoiceData>
    <SellerInformation><SellerName>{seller}</SellerName><SellerIdNum>{seller_id}</SellerIdNum></SellerInformation>
    <BuyerInformation><BuyerName>{buyer}</BuyerName><BuyerIdNum>{buyer_id}</BuyerIdNum></BuyerInformation>
    <BasicInformation>
      <TotalAmWithoutTax>{subtotal}</TotalAmWithoutTax>
      <TotalTaxAm>{tax}</TotalTaxAm>
      <TotalTax-includedAmount>{total}</TotalTax-includedAmount>
      <TotalTax-includedAmountInChinese>{words}</TotalTax-includedAmountInChinese>
      <RequestTime>2026-03-05 10:00:00</RequestTime>
    </BasicInformation>
    <IssuItemInformation>
      <ItemName>*电子元件*示例电阻</ItemName><MeaUnits>个</MeaUnits><Quantity>10</Quantity>
      <Amount>111.50</Amount><TaxRate>0.130000</TaxRate><ComTaxAm>14.50</ComTaxAm>
    </IssuItemInformation>
  </EInvoiceData>
  <TaxSupervisionInfo><IssueTime>2026-03-05</IssueTime></TaxSupervisionInfo>
</EInvoice>
"""


def write_xml(tmp_path, name="invoice.xml", **overrides):
    values = dict(
        invoice_no=INVOICE_NO, seller=SELLER_NAME, seller_id=SELLER_ID, buyer=BUYER_NAME, buyer_id=BUYER_ID,
        subtotal="111.50", tax="14.50", total="126.00", words="壹佰贰拾陆圆整",
    )
    values.update(overrides)
    path = tmp_path / name
    path.write_text(XML_TEMPLATE.format(**values), encoding="utf-8")
    return path


def test_xml_provides_combined_amounts_seller_tax_id_and_rates(tmp_path):
    invoice = parse_xml(write_xml(tmp_path))
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.total == Decimal("126.00") and invoice.total_present is True
    assert (invoice.subtotal, invoice.tax_total) == (Decimal("111.50"), Decimal("14.50"))
    assert invoice.total_in_words == Decimal("126.00")
    assert invoice.seller_tax_id == SELLER_ID
    assert invoice.tax_rates == [Decimal("13")]
    assert invoice.total_source == "xml"
    assert result.issues == []


def test_xml_that_does_not_close_is_reported(tmp_path):
    invoice = parse_xml(write_xml(tmp_path, tax="14.00", words="壹佰贰拾柒圆整"))
    result = check_invoice(invoice, context=CONTEXT)

    assert {"total_not_closed", "words_mismatch"} <= codes(result)
    assert {code for code, level in levels(result).items() if level == ISSUE_STRONG} == {
        "total_not_closed", "words_mismatch",
    }


def test_xml_missing_unreadable_and_zero_totals_are_different_things(tmp_path):
    missing = parse_xml(write_xml(tmp_path, "a.xml", total=""))
    unreadable = parse_xml(write_xml(tmp_path, "b.xml", total="N/A"))
    zero = parse_xml(write_xml(tmp_path, "c.xml", total="0.00", subtotal="0.00", tax="0.00", words="零圆整"))

    assert missing.total_present is False and unreadable.total_present is False
    assert zero.total_present is True and zero.total == Decimal("0.00")
    assert "未能识别发票总额" in check_invoice(missing, context=CONTEXT).message
    assert "发票总额「N/A」无法读取" in check_invoice(unreadable, context=CONTEXT).message
    # 0 是合法金额：不会被当作缺失，也不会因为合计字段为 0 触发闭合 / 大写提醒。
    assert not {c for c in codes(check_invoice(zero, context=CONTEXT)) if c.startswith(("total_", "words_"))}


def test_new_reminders_reach_the_stored_entry_without_a_schema_change(api, tmp_path):
    profile = api.create_profile("张三", "李老师")["data"]
    xml_path = write_xml(tmp_path, tax="14.00")

    entry = api.create_entry(profile["id"], xml_path=str(xml_path))["data"]

    assert entry["check_status"] == "warning"
    assert "不含税金额合计 111.50 加税额合计 14.00 为 125.50，与价税合计 126.00 不一致" in entry["check_message"]
    assert entry["seller"] == SELLER_NAME and entry["total"] == "126.00"


# --------------------------------------------------------------------------- 真实 PDF（reportlab 生成的虚构发票）

def make_pdf(path, lines):
    pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path))
    pdf.setFont("STSong-Light", 10)
    y = 800
    for line in lines:
        pdf.drawString(40, y, line)
        y -= 16
    pdf.save()
    return path


def pdf_lines(**overrides):
    values = dict(total="126.00", words="壹佰贰拾陆圆整", subtotal="111.50", tax="14.50")
    values.update(overrides)
    return [
        f"电子发票（普通发票） 发票号码：{INVOICE_NO}",
        "开票日期：2026年03月05日",
        f"名称：{BUYER_NAME}",
        f"统一社会信用代码/纳税人识别号：{BUYER_ID}",
        f"名称：{SELLER_NAME}",
        f"统一社会信用代码/纳税人识别号：{SELLER_ID}",
        "*电子元件*示例电阻 个 10 11.15 " + values["subtotal"] + " 13% " + values["tax"],
        f"合 计 ¥{values['subtotal']} ¥{values['tax']}",
        f"价税合计（大写） {values['words']} （小写）¥{values['total']}",
        "备注：订单金额 ¥1000.00",
    ]


def test_generated_pdf_is_read_by_label_and_passes_every_check(tmp_path):
    path = make_pdf(tmp_path / "ok.pdf", pdf_lines())

    invoice = parse_pdf(path, context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert invoice.invoice_no == INVOICE_NO
    assert invoice.total == Decimal("126.00")            # 不是备注里更大的 ¥1000.00
    assert invoice.total_source == TOTAL_SOURCE_LABEL
    assert invoice.field_checks["total"].state == FIELD_VERIFIED
    assert result.issues == []
    assert result.status == CHECK_PASS


def test_generated_pdf_with_inconsistent_amounts_produces_recognition_reminders(tmp_path):
    path = make_pdf(tmp_path / "bad.pdf", pdf_lines(tax="14.00", words="壹佰贰拾柒圆整"))

    invoice = parse_pdf(path, context=CONTEXT)
    result = check_invoice(invoice, context=CONTEXT)

    assert result.status == CHECK_WARNING
    assert {"total_not_closed", "words_mismatch"} <= codes(result)
    assert invoice.total == Decimal("126.00")


def test_xml_and_pdf_are_cross_checked_without_overwriting_xml(tmp_path, monkeypatch):
    from tidoc.engine import parse_invoice_files
    xml = tmp_path / 'invoice.xml'
    xml.write_text('<Invoice><EIid>26110000000000000001</EIid><SellerName>示例销售方</SellerName>'
                   '<TotalTax-includedAmount>126.00</TotalTax-includedAmount></Invoice>')
    monkeypatch.setattr(parser_module, 'parse_pdf', lambda *args, **kwargs: ParsedInvoice(
        invoice_no=OTHER_INVOICE_NO, seller='其他销售方', total=Decimal('120'), total_present=True))
    parsed = parse_invoice_files(xml, tmp_path / 'invoice.pdf', context=CONTEXT)
    assert parsed.invoice_no == INVOICE_NO
    assert parsed.total == Decimal('126')
    check = check_invoice(parsed, context=CONTEXT)
    assert {'invoice_no_conflict', 'seller_conflict', 'total_conflict'} <= codes(check)
    assert parsed.field_checks['total'].candidates == {'xml': '126.00', 'pdf': '120'}
    assert check.status == CHECK_WARNING


@pytest.mark.parametrize("failure", ["format", "dependency"])
def test_unreadable_pdf_does_not_discard_a_readable_xml(tmp_path, monkeypatch, failure):
    from tidoc.engine import parse_invoice_files
    xml = tmp_path / 'invoice.xml'
    xml.write_text('<Invoice><EIid>26110000000000000001</EIid><TotalTax-includedAmount>0.00</TotalTax-includedAmount></Invoice>')
    def unreadable(*args, **kwargs):
        from pypdf.errors import DependencyError
        raise DependencyError('encrypted PDF dependency missing') if failure == 'dependency' else ValueError('bad PDF')
    monkeypatch.setattr(parser_module, 'parse_pdf', unreadable)
    parsed = parse_invoice_files(xml, tmp_path / 'invoice.pdf')
    assert parsed.total_present is True
    assert parsed.source_warnings
    assert 'source_warning_0' in codes(check_invoice(parsed, context=CONTEXT))


def test_parser_evidence_survives_manual_correction_cloud_items_and_restart(api, parse_texts):
    from tidoc.services.ocr import refresh_entry_check, _replace_items
    from tidoc.db.entries import LOCAL_INVOICE_RECOGNITION_VERSION
    from tidoc.db import Database, EntryRepo
    parsed = parse_texts(
        plain_text(words='壹佰元整', invoice_no=OTHER_INVOICE_NO),
        layout_text(words='壹佰元整'),
    )
    profile = api.profiles.create('测试人员', '')
    entry_id = api.entries.create(profile['id'], parsed=parsed)
    # Blank title policy avoids unrelated organization warnings in this fixture.
    assert api.entries.get(entry_id)['seller_tax_id'] == SELLER_ID
    refresh_entry_check(api.entries, entry_id)
    before = api.entries.get(entry_id)['check_message']
    assert '大写金额' in before
    assert '发票号码识别到两个不同的结果' in before
    corrected = api.correct_locked_field(entry_id, 'invoice_no', INVOICE_NO)['data']
    assert '发票号码识别到两个不同的结果' not in corrected['check_message']
    assert '大写金额' in corrected['check_message']
    corrected_total = api.correct_locked_field(entry_id, 'total', '125.00')['data']
    assert '大写金额' in corrected_total['check_message']
    _replace_items(api.entries, entry_id, {'items': [{'name': '测试物品', 'total': '125'}]})
    assert '大写金额' in api.entries.get(entry_id)['check_message']
    db = Database(api.db.path) if hasattr(api.db, 'path') else Database(api.data_root.db_path)
    try:
        repo = EntryRepo(db)
        refresh_entry_check(repo, entry_id)
        assert '大写金额' in repo.get(entry_id)['check_message']
        db.conn.execute('UPDATE entries SET recognition_version=? WHERE id=?', ('invoice-local-2026-09-14-1', entry_id))
        db.conn.commit()
        assert LOCAL_INVOICE_RECOGNITION_VERSION != 'invoice-local-2026-09-14-1'
        assert repo.invoice_recognition_current(entry_id) is False
    finally:
        db.close()


def test_bindle_keeps_seller_tax_id_and_evidence_without_treating_them_as_a_local_cache(api, tmp_path, parse_texts):
    from tidoc.api import Api
    from tidoc.services.ocr import refresh_entry_check
    parsed = parse_texts(plain_text(words='壹佰元整'), layout_text(words='壹佰元整'))
    profile = api.profiles.create('测试人员', '')
    entry_id = api.entries.create(profile['id'], parsed=parsed)
    exported = api.export_bindle([entry_id])
    assert exported['ok'], exported
    target = Api(tmp_path / 'receiving')
    try:
        imported = target.import_bindle(exported['data']['path'], '')
        assert imported['ok'], imported
        imported_id = imported['data']['entry_ids'][0]
        entry = target.entries.get(imported_id)
        assert entry['seller_tax_id'] == SELLER_ID
        assert entry['recognition_version'] == ''
        refresh_entry_check(target.entries, imported_id)
        assert '大写金额' in target.entries.get(imported_id)['check_message']
        assert 'recognition_data' not in target.entries.list()[0]
    finally:
        target.db.close()


@pytest.mark.parametrize('broken', [
    {'total': 'NaN'}, {'tax_rates': ['Infinity']}, {'source_warnings': 'invalid'},
    {'seller_tax_id': []}, {'field_checks': []},
    {'field_checks': {'total': {'candidates': {'xml': []}}}},
    {'items': [{'name': [], 'actual_name': '', 'unit': '', 'total': '1'}]},
])
def test_binding_package_rejects_unusable_parser_evidence(broken):
    import json
    from tidoc.services.bindle import _validated_recognition_data
    with pytest.raises(ValueError, match='识别记录无法读取'):
        _validated_recognition_data(json.dumps(broken))


def test_image_only_pdf_is_reported_as_not_cross_checked(tmp_path):
    from pypdf import PdfWriter
    from tidoc.engine import parse_invoice_files
    xml = write_xml(tmp_path)
    pdf = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.write(pdf)
    parsed = parse_invoice_files(xml, pdf, context=CONTEXT)
    assert parsed.total == Decimal('126.00')
    assert 'source_warning_0' in codes(check_invoice(parsed, context=CONTEXT))
