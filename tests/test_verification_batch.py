"""AI 自动填写验证码的开关、单条自动查验流程和批量查验的收尾逻辑（不联网、不打开窗口）。"""

import sys
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from tests.conftest import enable_payment_ai
from tests.test_selection_performance import run_selection
from tests.test_verification_captcha import image_bytes
from tidoc import api_verification
from tidoc.api_verification import InvoiceVerificationApi
from tidoc.engine.models import ParsedInvoice
from tidoc.services import payment_ai, verification_captcha as captcha


@pytest.fixture
def supported(monkeypatch):
    """自动保存查验单只支持 macOS / Windows；测试里按支持的系统运行。"""
    monkeypatch.setattr(sys, "platform", "darwin")


def make_entry(api, **changes):
    profile = api.profiles.create("张三", "李老师")
    parsed = ParsedInvoice(**{
        "invoice_no": "26957000000168907686", "invoice_date": "2026-07-13",
        "total": Decimal("76.32"), **changes,
    })
    return api.entries.create(profile["id"], parsed=parsed)


# ----------------------------------------------------------------- 设置里的开关

def test_auto_captcha_switch_defaults_on_and_saves(api):
    loaded = api.invoice_verification_preferences()["data"]
    assert loaded["auto_captcha"] is True
    assert loaded["captcha_calls"] == 0 and loaded["captcha_cost"] == 0

    saved = api.set_invoice_verification_preferences({"auto_captcha": False})["data"]
    assert saved["auto_captcha"] is False
    assert api.invoice_verification_preferences()["data"] == saved
    assert api.settings_data()["data"]["verification"]["auto_captcha"] is False
    # 只改别的选项时，开关保持原状。
    api.set_invoice_verification_preferences({"trash_source_after_archive": True})
    assert api.invoice_verification_preferences()["data"]["auto_captcha"] is False
    assert api.set_invoice_verification_preferences({"auto_captcha": "1"})["data"]["auto_captcha"] is True


def test_switch_and_key_decide_whether_auto_captcha_is_offered(api, supported):
    entry_id = make_entry(api)
    assert api.invoice_verification_info(entry_id)["data"]["auto_available"] is False

    enable_payment_ai(api)
    assert api.invoice_verification_info(entry_id)["data"]["auto_available"] is True

    api.set_invoice_verification_preferences({"auto_captcha": False})
    assert api.invoice_verification_info(entry_id)["data"]["auto_available"] is False
    refused = api.start_invoice_verification(entry_id, None, True)
    assert refused["ok"] is False and "关闭" in refused["error"]
    refused = api.start_invoice_verification_batch([entry_id], 2)
    assert refused["ok"] is False and "关闭" in refused["error"]


def test_unsupported_system_is_reported_to_settings(api, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert api.invoice_verification_preferences()["data"]["auto_captcha_supported"] is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert api.invoice_verification_preferences()["data"]["auto_captcha_supported"] is True


def test_captcha_usage_is_counted_for_settings(api):
    session = {}
    api._record_captcha_usage({"cost": 0.002}, session)
    api._record_captcha_usage({"cost": None}, session)
    assert session["auto_cost"] == pytest.approx(0.002)
    prefs = api.invoice_verification_preferences()["data"]
    assert prefs["captcha_calls"] == 2
    assert prefs["captcha_cost"] == pytest.approx(0.002)


# ------------------------------------------------------------- 识别失败的分类

def run_recognize(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(payment_ai, "request_completion", fail)
    cfg = payment_ai.validate_config(payment_ai.DEFAULT_CONFIG)
    with pytest.raises(captcha.CaptchaError) as caught:
        captcha.recognize(cfg, "sk-test", image_bytes(), "请输入验证码图片中蓝色文字")
    return caught.value


@pytest.mark.parametrize("code, message", [(401, "Key 无效"), (403, "Key 无访问权限"), (402, "余额不足")])
def test_key_and_balance_errors_stop_the_attempts(monkeypatch, code, message):
    error = run_recognize(monkeypatch, HTTPError("https://example.test", code, "x", {}, None))
    assert error.fatal and str(error) == message


def test_rate_limit_and_network_errors_are_retried_after_a_pause(monkeypatch):
    limited = run_recognize(monkeypatch, HTTPError("https://example.test", 429, "x", {}, None))
    assert not limited.fatal and limited.pause >= captcha.RATE_LIMIT_PAUSE
    broken = run_recognize(monkeypatch, URLError("offline"))
    assert not broken.fatal and broken.pause == 0
    server = run_recognize(monkeypatch, HTTPError("https://example.test", 500, "x", {}, None))
    assert not server.fatal and "500" in str(server)


def test_missing_key_is_fatal():
    cfg = payment_ai.validate_config(payment_ai.DEFAULT_CONFIG)
    with pytest.raises(captcha.CaptchaError) as caught:
        captcha.recognize(cfg, "", image_bytes(), "")
    assert caught.value.fatal


# ----------------------------------------------------------- 单条自动查验循环

class FakeWindow:
    def __init__(self):
        self.events = SimpleNamespace(loaded=SimpleNamespace(wait=lambda timeout=None: True))


@pytest.fixture
def auto_runner(api, monkeypatch):
    """替换页面交互和识别，只验证循环如何计次、重试和收尾。"""
    enable_payment_ai(api)
    state = {"recognized": 0, "outcomes": [], "answers": [], "submitted": []}

    monkeypatch.setattr(InvoiceVerificationApi, "_auto_sleep", staticmethod(lambda session, seconds: True))
    monkeypatch.setattr(InvoiceVerificationApi, "_wait_verification_slot", staticmethod(lambda: None))
    monkeypatch.setattr(api, "_auto_wait_captcha", lambda session: {
        "image": "data:image/png;base64,AAAA", "hint": "", "checks": 0})
    monkeypatch.setattr(api, "_auto_wait_outcome", lambda session, before: state["outcomes"].pop(0))

    def fake_eval(window, method, argument=""):
        if method == "submit":
            state["submitted"].append(argument)
            return {"submitted": True}
        return {}

    monkeypatch.setattr(api, "_auto_eval", fake_eval)

    def fake_recognize(cfg, key, image, hint):
        state["recognized"] += 1
        answer = state["answers"].pop(0) if state["answers"] else {"text": "ab", "uncertain": False}
        if isinstance(answer, Exception):
            raise answer
        return {"cost": 0.001, "usage": {}, **answer}

    monkeypatch.setattr(captcha, "recognize", fake_recognize)

    def run():
        session = {"window": FakeWindow(), "auto_state": "starting", "auto_attempt": 0}
        api._run_auto_verification(session)
        return session

    state["run"] = run
    return state


def test_wrong_captcha_is_retried_then_succeeds(api, auto_runner):
    auto_runner["outcomes"] = [{"captcha_error": True}, {"success": True}]
    session = auto_runner["run"]()
    assert session["auto_state"] == "success"
    assert session["auto_attempt"] == 2
    assert session["auto_cost"] == pytest.approx(0.002)
    assert auto_runner["submitted"] == ['"ab"', '"ab"']
    assert api.invoice_verification_preferences()["data"]["captcha_calls"] == 2


def test_unreadable_captcha_is_not_submitted(api, auto_runner):
    auto_runner["answers"] = [{"text": "", "uncertain": True}, {"text": "xy", "uncertain": False}]
    auto_runner["outcomes"] = [{"success": True}]
    session = auto_runner["run"]()
    assert session["auto_state"] == "success" and session["auto_attempt"] == 2
    assert auto_runner["submitted"] == ['"xy"']


def test_invalid_key_stops_without_retrying(api, auto_runner):
    auto_runner["answers"] = [captcha.CaptchaError("Key 无效", fatal=True)]
    session = auto_runner["run"]()
    assert (session["auto_state"], session["auto_message"]) == ("failed", "Key 无效")
    assert auto_runner["recognized"] == 1


def test_transient_error_is_retried(api, auto_runner):
    auto_runner["answers"] = [captcha.CaptchaError("网络超时，请重试")]
    auto_runner["outcomes"] = [{"success": True}]
    session = auto_runner["run"]()
    assert session["auto_state"] == "success" and session["auto_attempt"] == 2


def test_gives_up_after_ten_attempts(api, auto_runner):
    auto_runner["outcomes"] = [{"captcha_error": True}] * api_verification.AUTO_MAX_ATTEMPTS
    session = auto_runner["run"]()
    assert session["auto_state"] == "failed"
    assert "10 次" in session["auto_message"]
    assert auto_runner["recognized"] == api_verification.AUTO_MAX_ATTEMPTS


def test_site_error_message_ends_the_attempts(api, auto_runner):
    auto_runner["outcomes"] = [{"error": "查验次数过多，请明日再查"}]
    session = auto_runner["run"]()
    assert (session["auto_state"], session["auto_message"]) == ("failed", "查验次数过多，请明日再查")


def test_waits_stop_as_soon_as_the_window_closes():
    started = time.monotonic()
    assert InvoiceVerificationApi._auto_sleep({"window_closed": True}, 30) is False
    assert InvoiceVerificationApi._auto_sleep({"auto_stop": True}, 30) is False
    assert InvoiceVerificationApi._auto_sleep({}, 0.05) is True
    assert time.monotonic() - started < 2


# --------------------------------------------------------------- 批量查验收尾

def new_job(api, *entry_ids):
    items = {entry_id: {"entry_id": entry_id, "state": "queued", "message": "",
                        "attempts": 0, "cost": 0.0} for entry_id in entry_ids}
    return {"id": "job", "total": len(items), "items": items, "order": list(items),
            "succeeded": 0, "failed": 0, "skipped": 0, "cancelled": False, "done": False,
            "started": time.monotonic(), "sessions": {}, "lock": threading.Lock()}


@pytest.fixture
def batch(api, monkeypatch):
    """用假的查验会话驱动 _run_batch_item，不打开窗口。"""
    monkeypatch.setattr(api_verification.time, "sleep", lambda seconds: None)
    context = {"status": {"ok": True, "data": {"state": "waiting"}}, "opened": [], "modes": {}}

    def open_session(entry_id, fields=None, auto=False, inspection_mode="ask", replace_id=""):
        context["modes"][entry_id] = inspection_mode
        session = {"id": "s-" + entry_id, "auto": True, "auto_state": "waiting",
                   "auto_attempt": 3, "auto_message": "第 3 次", "auto_cost": 0.004,
                   "window": None, "window_closed": False}
        session.update(context.get("session", {}))
        api._verification_sessions[session["id"]] = session
        context["opened"].append(entry_id)
        return {"session_id": session["id"]}

    monkeypatch.setattr(api, "_open_verification_session", open_session)
    monkeypatch.setattr(api, "invoice_verification_status", lambda session_id, options=None: context["status"])
    context["run"] = lambda job, entry_id: api._run_batch_item(job, entry_id)
    return context


def test_batch_item_saved(api, batch):
    job = new_job(api, "a")
    batch["status"] = {"ok": True, "data": {"state": "attached"}}
    batch["run"](job, "a")
    item = job["items"]["a"]
    assert (item["state"], item["message"]) == ("done", "查验单已保存")
    assert (job["succeeded"], job["failed"]) == (1, 0)
    assert item["cost"] == pytest.approx(0.004) and item["attempts"] == 3
    assert api._verification_sessions == {}, "成功后也要移除会话，不留窗口和临时目录"


def test_batch_item_reports_auto_failure(api, batch):
    job = new_job(api, "a")
    batch["session"] = {"auto_state": "failed", "auto_message": "余额不足", "auto_attempt": 2}
    batch["run"](job, "a")
    assert (job["items"]["a"]["state"], job["items"]["a"]["message"]) == ("failed", "余额不足")
    assert job["failed"] == 1 and api._verification_sessions == {}


def test_batch_item_does_not_wait_forever_on_a_status_error(api, batch):
    """查验过程中条目被补上了查验单等情况：状态接口返回错误，不能一直显示「自动查验中」。"""
    job = new_job(api, "a")
    batch["status"] = {"ok": False, "error": "当前条目已有查验单"}
    batch["run"](job, "a")
    assert (job["items"]["a"]["state"], job["items"]["a"]["message"]) == ("failed", "当前条目已有查验单")
    assert api._verification_sessions == {}


def test_batch_item_with_closed_window_is_failed_and_cleaned(api, batch):
    job = new_job(api, "a")
    batch["status"] = {"ok": True, "data": {"state": "window_closed", "window_closed": True}}
    batch["run"](job, "a")
    assert job["items"]["a"]["message"] == "查验窗口已关闭"
    assert api._verification_sessions == {}


def test_batch_item_rejected_auto_save_is_failed(api, batch):
    job = new_job(api, "a")
    batch["session"] = {"auto_save_rejected": True, "last_message": "自动生成的查验单正文未能确认发票号码"}
    batch["run"](job, "a")
    assert "未能确认发票号码" in job["items"]["a"]["message"]


def test_cancelled_batch_opens_nothing(api, batch):
    job = new_job(api, "a")
    job["cancelled"] = True
    batch["run"](job, "a")
    assert batch["opened"] == []
    assert job["items"]["a"]["state"] == "cancelled"
    assert (job["succeeded"], job["failed"]) == (0, 0)


def test_cancel_during_item_closes_its_window(api, batch):
    job = new_job(api, "a")

    def cancel_on_first_poll(session_id, options=None):
        job["cancelled"] = True
        return {"ok": True, "data": {"state": "waiting"}}

    api.invoice_verification_status = cancel_on_first_poll
    batch["run"](job, "a")
    assert job["items"]["a"]["state"] == "cancelled"
    assert api._verification_sessions == {}


def test_open_failure_marks_item_failed(api, batch):
    job = new_job(api, "a")

    def refuse(entry_id, fields=None, auto=False, **options):
        raise ValueError("无法打开查验平台窗口。")

    api._open_verification_session = refuse
    batch["run"](job, "a")
    assert job["items"]["a"]["message"] == "无法打开查验平台窗口。"
    assert job["failed"] == 1


def test_an_item_is_finished_only_once(api):
    job = new_job(api, "a")
    api._finish_batch_item(job, "a", "failed", "第一次")
    api._finish_batch_item(job, "a", "done", "不应覆盖")
    assert (job["items"]["a"]["state"], job["failed"], job["succeeded"]) == ("failed", 1, 0)


def test_batch_status_sums_cost_of_running_and_finished_items(api, supported):
    enable_payment_ai(api)
    first, second = make_entry(api), make_entry(api, invoice_no="26957000000168907687")
    done = threading.Event()

    def fake_run(job, entry_id):
        job["items"][entry_id]["cost"] = 0.003
        done.wait(5)

    api._run_batch_item = fake_run
    started = api.start_invoice_verification_batch([first, second], 2)["data"]
    time.sleep(0.2)
    status = api.invoice_verification_batch_status(started["id"])["data"]
    assert status["cost"] == pytest.approx(0.006)
    assert status["done"] is False
    assert {"invoice_no", "label"}.isdisjoint(status["items"][0])
    done.set()
    for _ in range(50):
        if api.invoice_verification_batch_status(started["id"])["data"]["done"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail("批量任务没有结束")


def test_batch_skips_incomplete_entries_and_refuses_a_second_job(api, supported):
    enable_payment_ai(api)
    complete = make_entry(api)
    incomplete = make_entry(api, invoice_no="26957000000168907687", total=None)
    release = threading.Event()
    api._run_batch_item = lambda job, entry_id: release.wait(5)

    started = api.start_invoice_verification_batch([complete, incomplete], 9)["data"]
    assert started["skipped"] == 1
    status = api.invoice_verification_batch_status(started["id"])["data"]
    states = {item["entry_id"]: item for item in status["items"]}
    assert states[incomplete]["state"] == "skipped"
    assert "缺少" in states[incomplete]["message"]

    again = api.start_invoice_verification_batch([complete], 2)
    assert again["ok"] is False and "等待" in again["error"]
    release.set()


def test_batch_refuses_more_than_the_limit(api, supported):
    enable_payment_ai(api)
    result = api.start_invoice_verification_batch([str(i) for i in range(api_verification.BATCH_MAX_ENTRIES + 1)], 2)
    assert result["ok"] is False and str(api_verification.BATCH_MAX_ENTRIES) in result["error"]


def add_inspection(api, entry_id, tmp_path, count=1, name="old.pdf", when="2026-10-29 13:56:27"):
    from tests.test_inspection_pdf import make_pdf

    return api.attachments.add(
        entry_id, make_pdf(tmp_path / name, count, when, number="26957000000168907686"), "inspection_pdf")


def run_batch(api, entry_ids, replace_existing=None):
    """启动批量任务但不运行条目，返回 (任务编号, 各条目状态)。"""
    ran = []
    api._run_batch_item = lambda job, entry_id: ran.append(entry_id)
    args = (entry_ids, 2) if replace_existing is None else (entry_ids, 2, replace_existing)
    started = api.start_invoice_verification_batch(*args)["data"]
    for _ in range(50):
        status = api.invoice_verification_batch_status(started["id"])["data"]
        if status["done"]:
            break
        time.sleep(0.05)
    return started, {item["entry_id"]: item for item in status["items"]}, ran


def test_batch_skips_entries_with_an_inspection_unless_told_to_replace(api, supported, tmp_path):
    enable_payment_ai(api)
    plain = make_entry(api)
    checked = make_entry(api, invoice_no="26957000000168907687")
    add_inspection(api, checked, tmp_path)

    started, items, ran = run_batch(api, [plain, checked])
    assert started["skipped"] == 1
    assert items[checked]["state"] == "skipped" and items[checked]["message"] == "已有查验单"
    assert ran == [plain]

    started, items, ran = run_batch(api, [plain, checked], True)
    assert started["skipped"] == 0
    assert items[checked]["replace"] is True and items[plain]["replace"] is False
    assert sorted(ran) == sorted([plain, checked])


def test_batch_of_only_checked_entries_can_still_start_when_replacing(api, supported, tmp_path):
    enable_payment_ai(api)
    checked = make_entry(api)
    add_inspection(api, checked, tmp_path)
    refused = api.start_invoice_verification_batch([checked], 2)
    assert refused["ok"] is False and "已有查验单" in refused["error"]
    started, items, ran = run_batch(api, [checked], "true")
    assert started["skipped"] == 0 and ran == [checked]


def test_replacing_never_rescues_an_entry_missing_invoice_data(api, supported, tmp_path):
    enable_payment_ai(api)
    plain = make_entry(api)
    incomplete = make_entry(api, invoice_no="26957000000168907687", total=None)
    add_inspection(api, incomplete, tmp_path)
    started, items, ran = run_batch(api, [plain, incomplete], True)
    assert items[incomplete]["state"] == "skipped" and "缺少" in items[incomplete]["message"]
    assert items[incomplete]["replace"] is False and ran == [plain]


def test_only_entries_that_had_an_inspection_replace_it(api, batch):
    job = new_job(api, "plain", "checked")
    job["items"]["checked"]["replace"] = True
    batch["status"] = {"ok": True, "data": {"state": "attached", "replaced": True,
                                            "inspection": {"count": 2, "count_scope": "day"}}}
    batch["run"](job, "plain")
    batch["run"](job, "checked")
    assert batch["modes"] == {"plain": "ask", "checked": "replace"}
    assert job["items"]["checked"]["message"] == "查验单已替换 · 当日第 2 次查验"


def test_saved_message_says_replaced_and_which_check_it_was():
    message = InvoiceVerificationApi._batch_saved_message
    assert message({}) == "查验单已保存"
    assert message({"inspection": {"count": None}}) == "查验单已保存"
    assert message({"inspection": {"count": 1, "count_scope": "unspecified"}}) == "查验单已保存 · 第 1 次查验"
    assert message({"replaced": True, "inspection": {"count": 3, "count_scope": "day"}}) == \
        "查验单已替换 · 当日第 3 次查验"


def test_verification_info_lists_existing_inspections_with_their_check_number(api, supported, tmp_path):
    entry_id = make_entry(api)
    assert api.invoice_verification_info(entry_id)["data"]["existing_inspections"] == []
    first = add_inspection(api, entry_id, tmp_path, 1, "one.pdf")
    second = add_inspection(api, entry_id, tmp_path, 2, "two.pdf", "2026-10-30 09:00:00")
    listed = api.invoice_verification_info(entry_id)["data"]["existing_inspections"]
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert listed[0]["name"] == "one.pdf" and listed[0]["count"] == 1
    assert listed[1]["count"] == 2 and listed[1]["checked_at"] == "2026-10-30 09:00:00"
    assert {"id", "name", "added_at", "count", "count_scope", "checked_at", "invoice_no"} <= set(listed[0])


def test_start_passes_only_a_known_save_choice_to_the_session(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_open_verification_session",
                        lambda entry_id, fields=None, auto=False, **options: calls.append((auto, options)) or {})
    api.start_invoice_verification("e", None, False)
    api.start_invoice_verification("e", None, True, {"mode": "replace", "replace_id": "att"})
    api.start_invoice_verification("e", None, False, {"mode": "keep"})
    api.start_invoice_verification("e", None, False, {"mode": "delete-everything"})
    api.start_invoice_verification("e", None, False, "replace")
    assert calls == [
        (False, {"inspection_mode": "ask", "replace_id": ""}),
        (True, {"inspection_mode": "replace", "replace_id": "att"}),
        (False, {"inspection_mode": "keep", "replace_id": ""}),
        (False, {"inspection_mode": "ask", "replace_id": ""}),
        (False, {"inspection_mode": "ask", "replace_id": ""}),
    ]


# --------------------------------------------------------------------- 前端

def test_batch_button_follows_switch_key_and_platform():
    run_selection(r"""
const button = makeNode();
document.querySelectorAll = (selector) => selector === '[data-verification-auto-ui]' ? [button] : [];
const hidden = () => { applyOcrUiVisibility(); return button.classList.contains('hidden'); };
State.verificationAutoCaptcha = true; State.verificationAutoSupported = true; State.paymentAiAvailable = true;
assert.equal(hidden(), false);
State.paymentAiAvailable = false;
assert.equal(hidden(), true, '没有 Key 时不显示');
State.paymentAiAvailable = true; State.verificationAutoCaptcha = false;
assert.equal(hidden(), true, '设置里关闭后不显示');
State.verificationAutoCaptcha = true; State.verificationAutoSupported = false;
assert.equal(hidden(), true, '系统不支持自动保存时不显示');
""")


def test_ai_captcha_switch_lives_in_developer_options_not_the_inspection_block():
    from tests.test_selection_performance import SOURCE

    source = SOURCE.read_text("utf-8")
    developer = source[source.index('id="setDevOptions"'):]
    developer = developer[:developer.index("</details>")]
    assert 'id="setVerificationAuto"' in developer
    assert "在线查验验证码 AI 识别" in developer
    archive = source[source.index("<!-- 查验单归档 -->"):source.index("<!-- 数据位置 -->")]
    assert "setVerificationAuto" not in archive
    assert "setVerificationWatchPick" in archive


def test_inspection_count_text_names_the_check_number_or_says_it_is_unreadable():
    run_selection(r"""
assert.equal(inspectionCountText({count: 1, count_scope: 'day', checked_at: '2026-10-29 13:56:27'}), '当日第 1 次查验 · 2026-10-29 13:56');
assert.equal(inspectionCountText({count: 2, count_scope: 'unspecified', checked_at: ''}), '第 2 次查验');
assert.equal(inspectionCountText({count: 3, count_scope: 'day', checked_at: '2026-10-29 13:56:27'}, false), '当日第 3 次查验');
assert.equal(inspectionCountText({count: null}), '未读出查验次数');
assert.equal(inspectionCountText(undefined), '未读出查验次数');
""")


def test_detail_marks_later_unreadable_and_foreign_inspections_as_reminders():
    run_selection(r"""
const hint = (inspection, entryNo = '123') => inspectionAttachmentHint({inspection}, entryNo);
const first = hint({count: 1, count_scope: 'day', checked_at: '2026-10-29 13:56:27', invoice_no: '123'});
assert.ok(first.includes('当日第 1 次查验 · 2026-10-29 13:56') && !first.includes('warn'));
const second = hint({count: 2, count_scope: 'day', checked_at: '', invoice_no: '123'});
assert.ok(second.includes('第 2 次查验') && second.includes('warn'));
const unreadable = hint({count: null, invoice_no: ''});
assert.ok(unreadable.includes('未读出查验次数') && unreadable.includes('请打开核对') && unreadable.includes('warn'));
const foreign = hint({count: 1, invoice_no: '999'});
assert.ok(foreign.includes('999') && foreign.includes('与条目不一致') && foreign.includes('warn'));
assert.equal(inspectionAttachmentHint({}, '123'), '');
// 发票号码读不出来时不能当作不一致。
assert.ok(!hint({count: 1, invoice_no: ''}).includes('不一致'));
""")


def test_card_badge_shows_ordinal_unreadable_and_file_count_only_when_there_is_something_to_see():
    run_selection(r"""
const badge = (inspection, open = true) => inspectionCardBadge({inspection, check_open: open ? ['inspection'] : []});
const chips = (inspection, open) => [...badge(inspection, open).html.matchAll(/>([^<]+)</g)].map(m => m[1]);
const one = {files: 1, count: 1, count_scope: 'day', checked_at: '2026-10-29 13:56:27'};
assert.deepEqual(chips(one), [], '第 1 次且只有一份：不显示');
assert.equal(badge(one).tip, '');
assert.deepEqual(chips(null), []);
assert.deepEqual(chips({files: 0}), []);
for (const [n, text] of [[2, '2nd'], [3, '3rd'], [4, '4th'], [11, '11th'], [12, '12th'], [13, '13th'], [21, '21st'], [22, '22nd'], [101, '101st'], [111, '111th']]) {
  assert.deepEqual(chips({...one, count: n}), [text]);
}
assert.deepEqual(chips({...one, count: null}), ['?']);
assert.deepEqual(chips({...one, files: 3}), ['×3'], '第 1 次但有多份：只显示份数');
assert.deepEqual(chips({...one, count: 4, files: 2}), ['4th', '×2']);
const tip = badge({...one, count: 4, files: 2}).tip;
assert.ok(tip.includes('当日第 4 次查验') && tip.includes('共 2 份查验单'));
assert.ok(badge({...one, count: null}).tip.includes('没有读出查验次数'));
// 提醒核对过后标记变灰，仍然显示。
assert.ok(badge({...one, count: 2}, true).html.includes('warn'));
assert.ok(!badge({...one, count: 2}, false).html.includes('warn'));
assert.deepEqual(chips({...one, count: 2}, false), ['2nd']);
""")

