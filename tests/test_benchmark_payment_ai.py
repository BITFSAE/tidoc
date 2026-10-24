"""Offline checks for benchmark safety and measurement semantics."""
from argparse import Namespace
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

from PIL import Image
import pytest

from scripts import benchmark_payment_ai as bench


def receipt(**changes):
    value = dict(source="bank", document_type="payment_receipt", status="success",
                 direction="expense", amount="400.00", currency="CNY", acceptable=True,
                 needs_review=False, reason="支出 400.00，余额未采用", evidence=["-¥400.00"])
    return dict(value, **changes)


def test_keys_are_deduplicated_and_json_arrays_validated(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEYS", '["one", "two", "one"]')
    assert bench.load_keys("qwen") == ["one", "two"]
    monkeypatch.setenv("DASHSCOPE_API_KEYS", '[1]')
    with pytest.raises(ValueError):
        bench.load_keys("qwen")


@pytest.mark.parametrize("url", ["http://example.com", "https://key@example.com", "https://example.com?key=x"])
def test_credentials_require_https_without_url_secrets(monkeypatch, url):
    monkeypatch.setenv("DASHSCOPE_BASE_URL", url)
    with pytest.raises(ValueError):
        bench.endpoint("qwen")


@pytest.mark.parametrize("changes", [dict(amount="-400.00"), dict(amount="0.00"),
    dict(status="pending"), dict(direction="income"), dict(needs_review=True),
    dict(source="other"), dict(acceptable=False)])
def test_invalid_or_inconsistent_decisions_are_errors(changes):
    with pytest.raises(Exception):
        bench.validate_result(receipt(**changes), ["bank"])


def test_unlabeled_samples_and_null_labels_are_distinct():
    assert bench.score(None, {}) == {}
    assert bench.score(None, {"amount": None}) == {"amount": False}
    assert bench.score({"amount": None}, {"amount": None}) == {"amount": True}


def test_compression_and_original_mode_do_not_change_source(tmp_path):
    path = tmp_path / "image.png"
    Image.new("RGBA", (1200, 2400), (0, 0, 0, 0)).save(path)
    before = path.read_bytes()
    sample = {"id": "test", "path": "image.png"}
    scaled = bench.prepare(sample, tmp_path, 800, 85)
    original = bench.prepare(sample, tmp_path, 0, 85)
    assert scaled.size == (400, 800)
    assert scaled.data_url.startswith("data:image/jpeg;base64,")
    assert original.data_url.startswith("data:image/png;base64,")
    assert original.sent_bytes == len(before)
    assert path.read_bytes() == before


def test_stream_requires_done_and_no_reasoning(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            return iter(lines)

    class Opener:
        def open(self, *args, **kwargs): return Response()

    from tidoc.services import payment_ai
    monkeypatch.setattr(payment_ai, "build_opener", lambda *args: Opener())
    chunk = {"choices": [{"delta": {"content": json.dumps(receipt())}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 25}, "model": "resolved"}
    lines = [("data: " + json.dumps(chunk)).encode(), b"data: [DONE]"]
    result = bench.request_completion("https://example.com", "not-a-real-key", {}, 2)
    assert result["result"] == receipt()
    assert result["usage"]["prompt_tokens"] == 25
    lines = lines[:-1]
    with pytest.raises(ValueError, match="incomplete"):
        bench.request_completion("https://example.com", "key", {}, 2)
    chunk["choices"][0]["delta"]["reasoning_content"] = "thinking"
    lines = [("data: " + json.dumps(chunk)).encode(), b"data: [DONE]"]
    with pytest.raises(ValueError, match="reasoning"):
        bench.request_completion("https://example.com", "key", {}, 2)


def test_cache_scope_and_no_cache_timing(monkeypatch, tmp_path):
    args = Namespace(cache=tmp_path, no_cache=False, allowed_sources=["bank"], requirements="",
                     retries=0, timeout=2)
    image = bench.Prepared("test", "digest", "data:image/png;base64,AA==", 1, 1, (1, 1), .01,
                           {"amount": "400.00"})
    calls = []
    def complete(url, key, payload, timeout):
        calls.append(key)
        return dict(result=receipt(), api_seconds=2, ttft_seconds=1, usage={}, resolved_model="test")
    monkeypatch.setattr(bench, "request_completion", complete)
    def run(key="secret-one"):
        slots = queue.Queue(); slots.put(key)
        result = bench.run_one(image, "deepseek-flash", "https://example.com", slots, args)
        assert slots.qsize() == 1
        return result
    first, cached = run(), run()
    assert len(calls) == 1 and cached["cached"]
    assert cached["api_seconds"] is None
    summary = bench.summarize([cached], .01)
    assert summary["api_success_per_minute"] is None and summary["api_median_seconds"] is None
    run("secret-two")
    args.requirements = "additional requirement"
    run()
    args.no_cache = True
    run()
    assert len(calls) == 4
    assert all("secret-one" not in p.read_text() for p in tmp_path.glob("*.json"))
    assert bench.summarize([first], 3)["api_success_per_minute"] == 20


def test_auth_failure_does_not_retry_or_expose_provider_body(monkeypatch, tmp_path):
    args = Namespace(cache=tmp_path, no_cache=True, allowed_sources=["bank"], requirements="",
                     retries=3, timeout=2)
    image = bench.Prepared("test", "digest", "data:image/png;base64,AA==", 1, 1, (1, 1), .01, {})
    calls = []
    def fail(*args):
        calls.append(1)
        raise HTTPError("https://example.com", 401, "provider text with secret", {}, None)
    monkeypatch.setattr(bench, "request_completion", fail)
    slots = queue.Queue(); slots.put("secret")
    row = bench.run_one(image, "deepseek-flash", "https://example.com", slots, args)
    assert len(calls) == 1 and row["result"] is None
    assert row["error"] == "HTTPError HTTP 401"
    assert "secret" not in json.dumps(row)


def test_partial_refund_net_is_checked_with_decimal():
    value = receipt(amount="27.00", gross_amount="43.20", refunded_amount="16.20",
                    status="refunded", acceptable=False, needs_review=True, theme="light")
    bench.validate_result(value, ["bank"], "compact")
    value["amount"] = "27.01"
    with pytest.raises(ValueError, match="net expense"):
        bench.validate_result(value, ["bank"], "compact")
    value.update(amount="0.00", refunded_amount="43.20")
    bench.validate_result(value, ["bank"], "compact")
    value.update(gross_amount=None, amount="16.20")
    with pytest.raises(ValueError, match="without original"):
        bench.validate_result(value, ["bank"], "compact")


def test_dark_theme_is_advisory_and_keeps_valid_receipt_acceptable():
    value = receipt(gross_amount="400.00", refunded_amount=None, theme="dark")
    bench.validate_result(value, ["bank"], "strict")
    assert value["acceptable"] and not value["needs_review"]


def test_profiles_explicitly_disable_thinking_and_keep_gross_and_refund():
    image = bench.Prepared("test", "digest", "data:image/png;base64,AA==", 1, 1, (1, 1), .01, {})
    for profile in ["strict", "compact"]:
        payload = bench.make_payload("deepseek-flash", image, ["bank"], "", profile)
        assert payload["thinking"] == {"type": "disabled"}
        prompt = payload["messages"][0]["content"][0]["text"]
        assert "gross_amount" in prompt and "refunded_amount" in prompt and "theme" in prompt


def extracted(**changes):
    return dict(dict(source="bank", kind="receipt", paid="43.20", refund=None,
                     completed=True, currency="CNY", theme="light", issue=""), **changes)


def test_fast_extraction_computes_net_and_review_without_model_arithmetic():
    result = bench.normalize_fast_result(extracted(refund="16.20"), ["bank"])
    assert result["amount"] == "27.00"
    assert not result["acceptable"] and result["needs_review"]
    assert result["gross_amount"] == "43.20" and result["refunded_amount"] == "16.20"
    assert bench.normalize_fast_result(extracted(paid="0.30", refund="0.10"), ["bank"])["amount"] == "0.20"
    assert bench.normalize_fast_result(extracted(refund="43.20"), ["bank"])["amount"] == "0.00"
    assert bench.normalize_fast_result(extracted(refund="43.21"), ["bank"])["amount"] is None
    assert bench.normalize_fast_result(extracted(kind="refund", paid=None, refund="16.20"), ["bank"])["amount"] is None
    result = bench.normalize_fast_result(extracted(kind="refund", refund="16.20"), ["bank"])
    assert result["amount"] == "27.00" and result["document_type"] == "payment_receipt"


def test_fast_order_and_income_cannot_be_applied_as_expense():
    for raw in [extracted(kind="order", paid="49.80"), extracted(kind="income"), extracted(completed=False)]:
        result = bench.normalize_fast_result(raw, ["bank"])
        assert result["amount"] is None and not result["acceptable"]


def test_fast_dark_is_advisory_but_requirements_and_source_are_enforced():
    dark = bench.normalize_fast_result(extracted(theme="dark"), ["bank"])
    assert dark["acceptable"] and not dark["needs_review"]
    for raw, allowed in [(extracted(), ["wechat"]), (extracted(issue="缺支付时间"), ["bank"])]:
        result = bench.normalize_fast_result(raw, allowed)
        assert not result["acceptable"] and result["needs_review"]


def test_multiple_keys_limit_inflight_requests_even_with_more_threads(monkeypatch, tmp_path):
    args = Namespace(cache=tmp_path, no_cache=True, allowed_sources=["bank"], requirements="",
                     retries=0, timeout=2)
    image = bench.Prepared("test", "digest", "data:image/png;base64,AA==", 1, 1, (1, 1), .01, {})
    lock = threading.Lock()
    current, peak, used = {}, {}, set()
    def complete(url, key, payload, timeout):
        with lock:
            used.add(key); current[key] = current.get(key, 0) + 1
            peak[key] = max(peak.get(key, 0), current[key])
        time.sleep(.02)
        with lock:
            current[key] -= 1
        return dict(result=receipt(), api_seconds=.02, ttft_seconds=.01, usage={})
    monkeypatch.setattr(bench, "request_completion", complete)
    slots = queue.Queue()
    for key in ["key-a", "key-b"] * 2:
        slots.put(key)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(bench.run_one, image, "deepseek-flash", "https://example.com", slots, args)
                   for _ in range(12)]
        assert all(f.result()["result"] is not None for f in futures)
    assert used == {"key-a", "key-b"} and max(peak.values()) <= 2
    assert slots.qsize() == 4


def test_fast_tolerates_harmless_json_wrappers_but_never_selects_from_multiple_results():
    value = extracted()
    assert bench.normalize_fast_result([value], ["bank"])["amount"] == "43.20"
    assert bench.normalize_fast_result(dict(value, type="json_object"), ["bank"])["amount"] == "43.20"
    for invalid in [[value, value], dict(value, type="refund"), dict(value, unknown="anything")]:
        with pytest.raises(Exception):
            bench.normalize_fast_result(invalid, ["bank"])


def test_consensus_requires_independent_majority_and_ignores_wording():
    def row(n, result, cached=False):
        return {"id": f"image#r{n}", "result": result, "cached": cached, "checks": {"amount": True}}
    a, b = receipt(), receipt(reason="不同表述", evidence=[])
    result = bench.consensus([row(1, a), row(2, b), row(3, None)], 3, 6)
    assert result["agreed"] == 1 and result["rows"][0]["votes"] == 2
    assert result["effective_images_per_minute"] == 10
    assert bench.consensus([row(1, a), row(2, None), row(3, None)], 3, 6)["undecided"] == 1
    assert bench.consensus([row(1, a, True), row(2, b, True), row(3, a, True)], 3, 6)["undecided"] == 1


def test_tiles_preserve_one_receipt_and_send_two_parts_without_ocr(tmp_path):
    image_path = tmp_path / "receipt.png"
    Image.new("RGB", (1200, 2800), "white").save(image_path)
    image = bench.prepare({"id": "test", "path": str(image_path)}, tmp_path, 1600, 85, "tiles")
    payload = bench.make_payload("deepseek-flash", image, ["bank"], "", "fast")
    content = payload["messages"][0]["content"]
    assert len(image.additional_data_urls) == 1
    assert len([p for p in content if p["type"] == "image_url"]) == 2
    assert "同一截图" in content[0]["text"]
