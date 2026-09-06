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
    assert 'id="searchClear"' in html
    assert ".search-clear" in css
    assert 'class="topbar-search"' not in html
    assert ".topbar-search" not in css
    assert 'class="search"' in html
    assert "toolbar-view-row" in html
    assert "toolbar-query-row" in html
    assert 'class="view-presets"' in html
    assert "tidoc.bindle.includeNotes" in source
    assert "tidoc.bindle.includeTags" in source
    assert "setBindleNotes" in source
    assert "setBindleTags" in source
    assert "已自动开启代填模式" in source
    assert 'id="setComponentsUpdate"' in source
    assert 'id="setPrintComponent"' not in source
    assert 'id="setUpdate"' not in source
    assert "软件与组件" in source
    assert "BV1XN3q69EPi" in source
    assert "vd_source=" not in source
    assert 'id="setBilibili"' in source
    assert "Api.listTitles" in source
    assert "refreshTitleOptions" in source
    assert "has-custom-title" in source
    assert "title-notice-dot" in html
    assert ".title-filter-chip.has-custom-title .title-notice-dot" in css
    assert "bindleNotesMode ? '包含' : '不包含'" not in source
    assert "autoUpdateMode ? '已开启' : '未开启'" not in source
    assert "updateEntryProfiles" in source
    card_source = source.split("function entryCard(e)", 1)[1].split(
        "function itemCardLabel", 1
    )[0]
    assert "batchBadges" in card_source
    assert "e.batches" in card_source
    assert "compBadge" not in card_source
    assert "${compBadge}" not in card_source
    assert 'id="changeProfileBtn"' in html
    assert 'id="batchReparseBtn"' in html
    assert 'id="filterUnbatched"' not in html
    assert "unbatched_count" in source
    assert 'class="batch-folder unbatched' in source
    assert "scopeChip('', '在办'" in source
    assert 'class="batch-folder-track"' in source
    assert 'class="batch-scope"' in source
    assert "archiveBatchFlow" in source
    assert "确认归档批次" in source
    assert 'id="selectAllBtn"' in html
    assert "toggleSelectAllVisible" in source
    assert "装入批次后可点击批次右侧 ⋯ 归档" in source
    assert "装入批次后可将批次归档" in source
    assert "点击批次右侧“⋯”可编辑批次、填写批次备注、归档" in source
    assert "batchNoteFlow" in source
    assert "batch-folder-note" in source
    assert "批次备注：" in source
    assert "focusedBatch?.note" in source
    assert "批次备注" in source
    assert "批次备注已保存" in source
    assert "批次备注（可选）" in source
    assert "已归档批次" in source
    assert "\u6536\u6863" not in source
    assert "inArchivedShelf()" in source
    assert "在办没有条目" in source
    assert "查看已归档" in source
    assert "当前要处理的条目都已完成批次并归档" in source
    assert 'id="archivedBatchesMenu"' not in source
    assert "已从在办收起" not in source
    assert "已归档，条目已从在办收起" not in source
    assert "f.active_only = true" in source
    assert "f.archived_only = true" in source
    assert "DOC_GUIDE_URL" in source
    assert "https://www.bitfsae.com/news/tidoc-guide" in source
    assert 'id="setDocGuide"' in source
    assert "说明文档" in source
    assert "setGuide').onclick = () => openUsageGuide(false)" in source
    assert "m.close(); openUsageGuide" not in source
    assert "actionBtn('pay', '付款'" in source
    assert "actionBtn('physical', '实物'" in source
    assert "showPhysicalAction" in source
    assert "左键点击条目上的批次标签" not in source
    assert "在卡片或详情添加付款截图、实物图和查验单" in source
    assert '<details class="settings-block">' in source
    assert ".att-group-actions" in css
    assert ".att-group.has .attach-item" in css
    assert ".archive-confirm-note" in css
    assert "State.quickView !== 'warning'" in source
    assert "Api.reparseEntries(ids)" in source
    assert "reparseEntries:" in (web / "api.js").read_text("utf-8")
    assert ".main { display: flex; flex-direction: column; min-width: 0; min-height: 0;" in css
    assert "flex: 1; min-height: 0; overflow-y: auto" in css


def test_card_drag_state_does_not_cover_amount_with_text():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    api_source = (app.web_dir() / "api.js").read_text("utf-8")
    styles = (app.web_dir() / "styles.css").read_text("utf-8")

    assert 'content: "拖到这里绑定材料"' not in styles
    assert "Api.setRecognizedPaidAmount" in source
    assert "setRecognizedPaidAmount:" in api_source


def test_bindle_preview_is_compact_and_keeps_legacy_profile_mapping():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    preview = source.split(
        "async function openBindleImportPreview", 1
    )[1].split("async function doImport", 1)[0]

    assert "legacyProfiles" in preview
    assert "entry.profile_name || fallback?.name" in preview
    assert 'name="bindleBatchMode"' in preview
    assert 'class="bindle-entry-head"' in preview
    assert "已读取 ${esc(baseName(path))}" not in preview
    assert "给全部导入条目添加标签" not in preview


def test_drag_and_paste_route_tidoc_to_bindle_preview():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    inbound = source.split(
        "async function openInboundBindle", 1
    )[1].split("async function addDroppedMaterialFiles", 1)[0]

    assert "n.endsWith('.tidoc')" in source
    assert "type === 'bindle_package'" in source
    assert "Api.inspectBindle(info.path)" in inbound
    assert "openBindleImportPreview(info.path, insp" in inbound
    assert "一次请只拖入或粘贴一个 .tidoc 文件" in inbound
    assert "onClose: () => cleanupDroppedPaths" in inbound
    assert source.count("await openInboundBindle(infos, paths, progress)") == 3


def test_export_names_include_local_time_and_settings_show_export_storage():
    source = (app.web_dir() / "app.js").read_text("utf-8")
    styles = (app.web_dir() / "styles.css").read_text("utf-8")

    assert "function filenameTimestamp" in source
    assert "'报账导出-' + filenameTimestamp()" in source
    assert "new Date().toISOString().slice(0, 10)" not in source
    export_dialog = source.split("async function doExport", 1)[1].split("function showExportResult", 1)[0]
    assert 'data-export="bindle" checked' in export_dialog
    assert 'data-export="excel" checked' not in export_dialog
    assert 'data-export="archive" checked' not in export_dialog
    assert "打开导出目录 · ${fmtBytes(maintenance.exports_size || 0)}" in source
    actions = source.split('<div class="settings-row-actions">', 1)[1].split("</div>", 1)[0]
    assert 'id="setOpenExports"' in actions
    assert 'id="setCleanup"' in actions
    settings_title = styles.split(".settings-block-title {", 1)[1].split("}", 1)[0]
    assert "font-family" not in settings_title
    font_sans = styles.split("--font-sans:", 1)[1].split(";", 1)[0]
    assert '"Microsoft YaHei UI"' in font_sans and '"Segoe UI"' in font_sans
    assert font_sans.index('"Segoe UI"') < font_sans.index('"Microsoft YaHei')
    assert "font-size: 12px" in settings_title
    assert "letter-spacing: 0" in settings_title
    assert "text-transform: none" in settings_title


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
    assert "等待约 1–2 秒" in verification_flow
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
