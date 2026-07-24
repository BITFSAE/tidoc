from pathlib import Path

from tidoc import __version__
from tidoc import app


def test_web_dir_prefers_pyinstaller_meipass(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle" / "tidoc" / "web"
    bundled.mkdir(parents=True)
    (bundled / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(app.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert app.web_dir() == bundled


def test_web_dir_uses_source_tree_without_bundle(monkeypatch):
    monkeypatch.delattr(app.sys, "_MEIPASS", raising=False)

    assert (app.web_dir() / "index.html").is_file()


def test_web_app_url_and_assets_are_versioned(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    source = (app.web_dir() / "index.html").read_text("utf-8")

    assert app.web_app_url(index) == index.as_uri()
    assert f"styles.css?v={__version__}" in source
    assert f"api.js?v={__version__}" in source
    assert f"app.js?v={__version__}" in source


def test_loose_material_matching_does_not_reuse_previous_import_scope():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    loose_handler = source.split(
        "async function handleLooseMaterialInfos", 1
    )[1].split("function readFileAsDataURL", 1)[0]

    assert "autoBindMaterialInfos(infos, []," in loose_handler
    assert "recentImportedEntryIds" not in source
    assert "pendingMaterialInfos.length && createdEntries.length" in source
    assert "autoBindMaterialInfos(pendingMaterialInfos, createdEntries)" in source
    assert '<b>未创建：</b>${esc(g.error)}' in source
    assert "未创建：${r.failed[0].error}" in source


def test_frontend_has_payment_ocr_setting_batch_profile_and_scroll_constraints():
    web = app.web_dir()
    source = (web / "app.js").read_text("utf-8")
    html = (web / "index.html").read_text("utf-8")
    css = (web / "styles.css").read_text("utf-8")

    assert "tidoc.paymentScreenshotOcr" in source
    assert "tidoc.defaultPaidToInvoiceTotal" in source
    assert "setDefaultPaidInvoice" in source
    assert "updateEntryProfiles" in source
    assert 'id="changeProfileBtn"' in html
    assert ".main { display: flex; flex-direction: column; min-width: 0; min-height: 0;" in css
    assert "flex: 1; min-height: 0; overflow-y: auto" in css


def test_card_drag_state_does_not_cover_amount_with_text():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    api_source = (app.web_dir() / "api.js").read_text("utf-8")
    styles = (app.web_dir() / "styles.css").read_text("utf-8")

    assert 'content: "拖到这里绑定材料"' not in styles
    assert "Api.setRecognizedPaidAmount" in source
    assert "setRecognizedPaidAmount:" in api_source


def test_single_payment_amount_mismatch_requires_confirmation():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    settle = source.split(
        "async function settlePaymentAmountAfterAdd", 1
    )[1].split("function moneyText", 1)[0]
    prompt = source.split(
        "function askUseRecognizedPaymentAmount", 1
    )[1].split("function openEntryMenu", 1)[0]

    assert "differsFromInvoice" in settle
    assert "paymentInfos.length === 1 && amounts.length === 1 && !differsFromInvoice" in settle
    assert "paidField.value_source !== 'payment_ocr'" in settle
    assert "与截图金额不一致，请确认" in prompt
    assert "保持当前 ${fmtMoney(currentPaid)}" in prompt
    assert "按截图 ${fmtMoney(sum)}" in prompt
