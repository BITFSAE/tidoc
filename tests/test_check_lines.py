"""识别提醒按类别归类，可逐条标为已核对；内容变了重新出现。"""

from decimal import Decimal

import pytest

from tidoc.engine import check_invoice
from tidoc.engine.check_lines import check_lines, line_category, open_categories
from tidoc.engine.models import ParsedInvoice, ParsedItem
from tests.test_inspection_pdf import new_entry
from tests.test_selection_performance import run_selection


def test_tagged_and_legacy_lines_get_categories():
    assert line_category("[发票金额] 未能识别发票总额，请手动填写。") == "amount"
    assert line_category("[付款截图识别] 付款截图识别合计 3.00 与实付 10.00 不一致，请核对。") == "payment"
    assert line_category("[查验单] 当前条目有 2 份查验单，请核对并删除不需要的材料。") == "inspection"
    # 早期写入的提醒没有标记，按内容归类。
    assert line_category("明细识别合计与发票总额相差 1.00，可能是明细识别不完整，请以发票总额为准。") == "amount"
    assert line_category("购买方税号「1」与「某单位」不一致，应为 2，请核对。") == "title"
    assert line_category("销售方税号「1」的校验位不符，可能识别有误，请核对。") == "invoice"
    assert line_category("来自其他报账方案的材料") == "other"
    # 未知的方括号开头不当作类别标记，原样显示。
    assert check_lines("[备注] 文字")[0]["text"] == "[备注] 文字"


def test_validator_tags_every_line_with_its_category():
    parsed = ParsedInvoice(invoice_no="1", total=Decimal("10.00"), buyer_name="")
    lines = check_lines(check_invoice(parsed).message)
    assert {line["category"] for line in lines} >= {"invoice", "title"}
    assert all(not line["text"].startswith("[") for line in lines)
    parsed = ParsedInvoice(
        invoice_no="1", total=Decimal("10.00"), buyer_name="甲单位",
        items=[ParsedItem("物品", "物品", "个", Decimal("1"), Decimal("9.00"))],
    )
    lines = check_lines(check_invoice(parsed, expected_title="乙单位").message)
    categories = [line["category"] for line in lines]
    assert "amount" in categories and "title" in categories


def test_open_categories_follow_status_and_review():
    lines = check_lines("[发票金额] a；[抬头税号] b", '["a"]')
    assert open_categories("warning", lines) == ["title"]
    assert open_categories("blocked", lines) == []
    assert open_categories("pass", []) == []
    assert open_categories("warning", []) == ["other"]


def test_review_hides_entry_from_warning_view_until_reminder_changes(api):
    entry_id = new_entry(api)
    api.entries.set_check(entry_id, "warning", "[发票金额] 金额不一致；[抬头税号] 抬头未识别。")
    assert api.entries.get(entry_id)["check_open"] == ["amount", "title"]

    result = api.review_check_lines(entry_id, ["金额不一致"], True, "")
    assert result["ok"], result
    entry = result["data"]
    assert entry["check_open"] == ["title"]
    assert [line["reviewed"] for line in entry["check_lines"]] == [True, False]
    assert entry["check_status"] == "warning"
    assert any(row["field"] == "识别提醒已核对" and row["new_value"] == "金额不一致"
               for row in entry["history"])

    api.entries.review_check_lines(entry_id, ["抬头未识别。", "已经不存在的提醒"], True)
    listed = {entry["id"]: entry for entry in api.entries.list(check_status="warning")}
    assert listed[entry_id]["check_open"] == []

    # 撤销后重新列入；提醒内容变化后，旧的核对不再适用。
    api.entries.review_check_lines(entry_id, ["抬头未识别。"], False)
    assert api.entries.get(entry_id)["check_open"] == ["title"]
    api.entries.review_check_lines(entry_id, ["抬头未识别。"], True)
    api.entries.set_check(entry_id, "warning", "[发票金额] 金额相差 2.00；[抬头税号] 抬头未识别。")
    assert api.entries.get(entry_id)["check_open"] == ["amount"]


def test_batch_review_marks_every_open_line_and_skips_blocked(api):
    first, second, blocked = new_entry(api), new_entry(api), new_entry(api)
    api.entries.set_check(first, "warning", "[发票金额] 金额不一致；[抬头税号] 抬头未识别。")
    api.entries.set_check(second, "warning", "[发票信息] 日期未识别。")
    api.entries.set_check(blocked, "blocked", "[抬头税号] 与当前分区不一致，禁止混入。")
    api.entries.review_check_lines(second, ["日期未识别。"], True)

    result = api.review_all_check_lines([first, second, blocked, first], "")
    assert result["ok"], result
    assert result["data"] == {"entries": 1, "lines": 2}
    assert api.entries.get(first)["check_open"] == []
    assert api.entries.get(blocked)["check_lines"][0]["reviewed"] is False


def test_blocked_entries_cannot_be_reviewed(api):
    entry_id = new_entry(api)
    api.entries.set_check(entry_id, "blocked", "[抬头税号] 与当前分区不一致，禁止混入。")
    with pytest.raises(ValueError):
        api.entries.review_check_lines(entry_id, ["与当前分区不一致，禁止混入。"], True)


def test_warning_view_counts_and_filters_by_category():
    run_selection(r"""
State.quickView = 'warning';
const entries = [
  {id: 'a', check_open: ['amount', 'payment'], check_lines: [{reviewed: true}]},
  {id: 'b', check_open: ['payment']},
  {id: 'c', check_open: ['title']},
  {id: 'd', check_open: [], check_lines: [{reviewed: true}]},
];
State.checkCategory = '';
assert.deepEqual(applyCheckCategory(entries).map(e => e.id), ['a', 'b', 'c']);
assert.deepEqual(State.checkCategoryCounts, {'': 3, reviewed: 2, amount: 1, payment: 2, title: 1});
State.checkCategory = 'payment';
assert.deepEqual(applyCheckCategory(entries).map(e => e.id), ['a', 'b']);
// 「已核对」列出核对过至少一条提醒的条目，包括还有别的提醒没核对的。
State.checkCategory = 'reviewed';
assert.deepEqual(applyCheckCategory(entries).map(e => e.id), ['a', 'd']);
State.quickView = 'all';
assert.equal(applyCheckCategory(entries).length, 4);
assert.equal(State.checkCategoryCounts, null);
const badge = entryCheckBadge({check_status: 'warning', check_open: ['amount', 'payment'],
  check_lines: [{text: 'x', category: 'amount', reviewed: false}, {text: 'y', category: 'payment', reviewed: false},
    {text: 'z', category: 'title', reviewed: true}]});
assert.ok(badge.includes('发票金额等 2 类'));
assert.ok(badge.includes('data-card-check'));
assert.ok(!badge.includes('z'));
const reviewed = entryCheckBadge({check_status: 'warning', check_open: [], check_lines: [{text: 'z', category: 'title', reviewed: true}]});
assert.ok(reviewed.includes('已核对') && reviewed.includes('data-card-check') && reviewed.includes('z'));
assert.equal(entryCheckBadge({check_status: 'pass', check_open: [], check_lines: []}), '');
""")
