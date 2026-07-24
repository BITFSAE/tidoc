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


def test_webview_settings_allow_tax_site_certificate_fallback():
    import webview

    original_downloads = webview.settings["ALLOW_DOWNLOADS"]
    original_ssl = webview.settings["IGNORE_SSL_ERRORS"]
    try:
        app._configure_webview_settings()
        assert webview.settings["ALLOW_DOWNLOADS"] is True
        assert webview.settings["IGNORE_SSL_ERRORS"] is True
    finally:
        webview.settings["ALLOW_DOWNLOADS"] = original_downloads
        webview.settings["IGNORE_SSL_ERRORS"] = original_ssl


def test_normal_startup_filters_known_native_and_pdf_noise():
    patterns = "\n".join(app._STDERR_SUPPRESS_PATTERNS)
    assert "IMKCFRunLoopWakeUpReliable" in patterns
    assert "Ignoring wrong pointing object" in patterns


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


def test_frontend_exposes_explicit_online_verification_flow():
    web = app.web_dir()
    source = (web / "app.js").read_text("utf-8")
    api_source = (web / "api.js").read_text("utf-8")
    app_source = (Path(app.__file__)).read_text("utf-8")
    verification_flow = source.split(
        "async function onlineVerificationFlow", 1
    )[1].split("async function maybeSetPaidFromInvoice", 1)[0]

    assert "async function onlineVerificationFlow" in source
    assert "<ul>" in verification_flow
    assert "验证码在官网填写，通常不区分大小写" in verification_flow
    assert "查验成功后点击官网“打印”" in verification_flow
    assert "设置中的归档目录" in verification_flow
    assert "archiveLocationHint" in verification_flow
    assert "VERIFICATION_WATCH_DIR_KEY" in source
    assert "data-verification-watch-pick" not in verification_flow
    assert "watch_directory: watchDirectory" not in verification_flow
    assert "归档后清理原 PDF" in source
    assert "Api.invoiceVerificationPreferences" in source
    assert "Api.setInvoiceVerificationPreferences" in source
    assert "invoiceVerificationPreferences:" in api_source
    assert "setInvoiceVerificationPreferences:" in api_source
    assert "网页证书错误" not in verification_flow
    assert "无需打印机" not in verification_flow
    assert "价税合计" in verification_flow
    assert "invoice_code" not in verification_flow
    assert "Api.invoiceVerificationStatus" in source
    assert "Api.saveInvoiceVerificationPdf" not in source
    assert "saveInvoiceVerificationPdf" not in api_source
    assert "printInvoiceVerification" not in api_source
    assert "startInvoiceVerification:" in api_source
    assert 'webview.settings["ALLOW_DOWNLOADS"] = True' in app_source
    assert 'webview.settings["IGNORE_SSL_ERRORS"] = True' in app_source


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
