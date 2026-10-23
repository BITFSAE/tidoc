"""Bridge envelopes, preference keys and filesystem containment helpers."""
import functools
from pathlib import Path

AUTO_UPDATE_PREF_KEY = "tidoc.update.autoCheck"
UPDATE_CHANNEL_PREF_KEY = "tidoc.update.channel"  # stable（默认）或 beta
PAYMENT_OCR_PREF_KEY = "tidoc.paymentScreenshotOcr"
OCR_ACCESS_KEY_ID_PREF_KEY = "tidoc.ocr.accessKeyId"
OCR_ACCESS_KEY_SECRET_PREF_KEY = "tidoc.ocr.accessKeySecret"
DEFAULT_PAID_TO_INVOICE_PREF_KEY = "tidoc.defaultPaidToInvoiceTotal"
DEFAULT_ENTRY_TITLE_PREF_KEY = "tidoc.defaultEntryTitle"
TITLE_PROFILES_PREF_KEY = "tidoc.titleProfiles"
BINDLE_INCLUDE_NOTES_PREF_KEY = "tidoc.bindle.includeNotes"
BINDLE_INCLUDE_TAGS_PREF_KEY = "tidoc.bindle.includeTags"
SHOW_CREATED_AT_PREF_KEY = "tidoc.cards.showCreatedAt"
INVOICE_VERIFICATION_WATCH_DIR_PREF_KEY = (
    "tidoc.invoiceVerification.watchDirectory"
)
INVOICE_VERIFICATION_TRASH_SOURCE_PREF_KEY = (
    "tidoc.invoiceVerification.trashSourceAfterArchive"
)
UPDATE_LAST_CHECK_KEY = "tidoc.update.lastCheck"
UPDATE_LAST_RESULT_KEY = "tidoc.update.lastResult"
APP_LAST_SEEN_VERSION_KEY = "tidoc.update.lastSeenVersion"
AUTO_UPDATE_INTERVAL_SECONDS = 60 * 60
MAX_DROPPED_FILE_BYTES = 100 * 1024 * 1024
MAX_DROPPED_TOTAL_BYTES = 500 * 1024 * 1024


class DuplicateInvoiceError(ValueError):
    """导入命中了已有发票，携带可供前端定位原条目的结构化信息。"""

    def __init__(self, existing: dict):
        self.existing = existing
        invoice_no = existing.get("invoice_no") or ""
        seller = existing.get("seller") or "未识别销售方"
        claimant = existing.get("profile_name") or "未识别报账人"
        identity = f"发票号 {invoice_no} 已存在" if invoice_no else "相同发票文件已存在"
        super().__init__(
            f"{identity}（{seller}，报账人：{claimant}）。"
            "如需调整归属，请修改原条目的报账人，不要重复创建。"
        )


def _guard(func):
    """把返回值包成 {ok:True,...}，异常包成 {ok:False,error:...}。"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            with self._api_lock:
                result = func(self, *args, **kwargs)
            # 已经是 {ok, data|error} 形式的结果原样返回；但预检计划这类业务数据本身带一个
            # ok 字段（有阻断项时为 False，且没有 error），必须作为数据交给前端，否则前端会把
            # 它当成调用失败，只显示「未知错误」，看不到阻断原因。
            if isinstance(result, dict) and "ok" in result and (result["ok"] or "error" in result):
                return result
            return {"ok": True, "data": result}
        except Exception as exc:  # noqa: BLE001 — 桥不能抛，统一转错误
            error = {"ok": False, "error": str(exc)}
            if getattr(exc, "diagnostics", None):
                error["diagnostics"] = exc.diagnostics
            return error
    return wrapper


def _is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_empty_dropped_parent(root: Path, folder: Path) -> None:
    if folder.resolve() == root.resolve() or not _is_inside(root, folder):
        return
    try:
        folder.rmdir()
    except OSError:
        pass
