"""User-triggered tax website verification."""
from __future__ import annotations
import sys
import threading
import uuid
from pathlib import Path
from send2trash import send2trash
from .api_shared import _guard, _is_inside


class InvoiceVerificationApi:
    @_guard
    def invoice_verification_info(self, entry_id):
        """从已有发票材料准备官网查验表单，但不主动联网。"""
        from .services.invoice_verification import build_verification_info

        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")
        return build_verification_info(entry)

    @_guard
    def start_invoice_verification(self, entry_id, fields=None):
        """用户主动打开税务官网，预填发票信息并创建结果保存会话。"""
        import webview

        from .services.invoice_verification import (
            TAX_VERIFICATION_URL,
            build_verification_info,
            make_prefill_script,
            make_print_compatibility_script,
            new_session_snapshot,
            snapshot_pdfs,
            verification_print_title,
        )

        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")
        info = build_verification_info(entry)
        for key in ("invoice_no", "invoice_date", "verification_value"):
            if fields and fields.get(key) is not None:
                info[key] = str(fields[key]).strip()
        if not info["invoice_no"]:
            raise ValueError("缺少发票号码，请先在条目详情补正发票号码。")
        if not info["invoice_date"]:
            raise ValueError("缺少开票日期，请先在条目详情补正开票日期。")

        session_id = uuid.uuid4().hex
        snapshot = new_session_snapshot()
        verification_preferences = self._invoice_verification_preferences()
        custom_watch = verification_preferences["watch_directory"]
        if custom_watch:
            custom_watch_path = Path(custom_watch).expanduser()
            if not custom_watch_path.is_dir():
                raise ValueError("设置中的查验单归档目录不存在，请重新选择。")
            custom_watch_path = custom_watch_path.resolve()
            if _is_inside(self.data_root.root, custom_watch_path):
                raise ValueError("查验单归档目录不能位于 tidoc 数据目录内。")
            if custom_watch_path not in snapshot["watch_directories"]:
                snapshot["watch_directories"].append(custom_watch_path)
                snapshot["before"].update(snapshot_pdfs([custom_watch_path]))
        session = {
            "id": session_id,
            "entry_id": entry_id,
            "invoice_no": info["invoice_no"],
            "window": None,
            "window_closed": False,
            "attached": None,
            "candidate_sizes": {},
            "seen_candidates": set(),
            "last_message": "",
            "result_message": "",
            "cleanup_warning": "",
            "source_trashed": False,
            "trash_source_after_archive": verification_preferences[
                "trash_source_after_archive"
            ],
            **snapshot,
        }
        self._verification_sessions[session_id] = session
        try:
            self._install_verification_landscape_print()
            window = webview.create_window(
                "tidoc · 发票查验",
                url=TAX_VERIFICATION_URL,
                width=1360,
                height=860,
                min_size=(1000, 680),
                text_select=True,
                zoomable=True,
            )
            if window is None:
                raise RuntimeError("无法打开查验平台窗口。")
            window._tidoc_print_title = verification_print_title(  # type: ignore[attr-defined]
                info["invoice_no"]
            )
            session["window"] = window

            def on_window_closed() -> None:
                with self._api_lock:
                    session["window_closed"] = True

            window.events.closed += on_window_closed
            script = make_prefill_script(info)
            print_compatibility_script = make_print_compatibility_script(
                info["invoice_no"]
            )

            def install_print_compatibility() -> None:
                if session["window_closed"]:
                    return
                try:
                    window.evaluate_js(print_compatibility_script)
                except Exception:  # noqa: BLE001 - 不阻断官网正常浏览
                    session["last_message"] = (
                        "官网已打开，但打印兼容处理失败；可关闭后重新打开查验窗口。"
                    )

            def prepare_verification_page() -> None:
                if session["window_closed"]:
                    return
                try:
                    window.evaluate_js(script)
                except Exception:  # noqa: BLE001 - 官网仍可供用户手动填写
                    session["last_message"] = "官网已打开，但自动填写失败，请在官网手动填写。"
                install_print_compatibility()

            # 旧地址会重定向到新版单页应用；每次加载都准备，脚本按文档去重。
            # 官网查验成功后的结果页也需要重新安装打印兼容处理。
            window.events.loaded += prepare_verification_page

            def prefill_when_loaded() -> None:
                if window.events.loaded.wait(30):
                    prepare_verification_page()

            threading.Thread(target=prefill_when_loaded, daemon=True).start()
        except Exception:
            self._verification_sessions.pop(session_id, None)
            raise

        return {
            "session_id": session_id,
            "watch_directories": [str(path) for path in session["watch_directories"]],
            "info": info,
        }

    @_guard
    def invoice_verification_status(self, session_id):
        """检测用户刚保存的官网查验单，确认归属后直接绑定到当前条目。"""
        from .db import TYPE_INSPECTION
        from .services.folder_import import (
            classify_pdf_attachment_type,
            extract_pdf_invoice_no,
        )
        from .services.invoice_verification import changed_pdf_candidates

        session = self._verification_sessions.get(str(session_id))
        if not session:
            raise ValueError("查验会话已结束，请重新打开查验平台。")
        if session["attached"]:
            return {
                "state": "attached",
                "attachment": session["attached"],
                "message": (
                    session.get("result_message")
                    or "查验单已保存到当前条目。"
                ),
                "cleanup_warning": session.get("cleanup_warning", ""),
                "source_trashed": bool(session.get("source_trashed")),
            }

        candidates = changed_pdf_candidates(
            session["watch_directories"],
            session["before"],
            session["started_ns"],
        )
        waiting_for_write = False
        for path in candidates:
            key = str(path)
            if key in session["seen_candidates"]:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            previous_size = session["candidate_sizes"].get(key)
            session["candidate_sizes"][key] = size
            if previous_size != size or size == 0:
                waiting_for_write = True
                continue
            session["seen_candidates"].add(key)
            try:
                if classify_pdf_attachment_type(path) != TYPE_INSPECTION:
                    continue
                detected_invoice_no = extract_pdf_invoice_no(path)
                if (detected_invoice_no and session["invoice_no"]
                        and detected_invoice_no != session["invoice_no"]):
                    session["last_message"] = (
                        f"发现一份其他发票的查验单（{detected_invoice_no}），未绑定。"
                    )
                    continue
                if session["invoice_no"] and not detected_invoice_no:
                    session["last_message"] = (
                        "发现一份查验单，但未能确认发票号码，未自动绑定。"
                        "原 PDF 已保留，请手动选择这份文件。"
                    )
                    continue
                self._validate_attachment_for_entry(
                    session["entry_id"], path, TYPE_INSPECTION
                )
                attachment = self.attachments.add(
                    session["entry_id"], path, TYPE_INSPECTION,
                    note="由全国增值税发票查验平台原生打印保存",
                )
                self.entries.recompute_status(session["entry_id"])
                session["attached"] = attachment
                result_message = "查验单已保存到当前条目。"
                cleanup_warning = ""
                source_trashed = False
                if session.get("trash_source_after_archive"):
                    try:
                        send2trash(str(path))
                        source_trashed = True
                        result_message = (
                            "查验单已保存，原 PDF 已移到废纸篓或回收站。"
                        )
                    except Exception as exc:  # noqa: BLE001 - 归档成功不应回滚
                        cleanup_warning = (
                            "原 PDF 未能移到废纸篓或回收站，仍保留在原位置："
                            f"{exc}"
                        )
                        result_message = (
                            "查验单已保存，但原 PDF 仍保留在原位置。"
                        )
                session["result_message"] = result_message
                session["cleanup_warning"] = cleanup_warning
                session["source_trashed"] = source_trashed
                self._close_verification_window(session)
                return {
                    "state": "attached",
                    "attachment": attachment,
                    "message": result_message,
                    "cleanup_warning": cleanup_warning,
                    "source_trashed": source_trashed,
                }
            except Exception as exc:  # noqa: BLE001 - 继续等待用户保存正确文件
                session["last_message"] = f"发现 PDF，但未能绑定：{exc}"

        state = "processing" if waiting_for_write else (
            "window_closed" if session["window_closed"] else "waiting"
        )
        return {
            "state": state,
            "message": session["last_message"],
            "window_closed": session["window_closed"],
        }

    @_guard
    def close_invoice_verification(self, session_id):
        session = self._verification_sessions.pop(str(session_id), None)
        if session:
            self._close_verification_window(session)
        return {"closed": bool(session)}

    def _close_verification_window(self, session: dict) -> None:
        window = session.get("window")
        if window is None or session.get("window_closed"):
            return
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - 用户可能已先关闭窗口
            pass
        session["window_closed"] = True

    @staticmethod
    def _configure_verification_print_info(info) -> None:
        """把当前原生打印任务固定为横向。"""
        if sys.platform != "darwin":
            return
        try:
            import AppKit
        except ImportError:
            return
        info.setOrientation_(AppKit.NSPaperOrientationLandscape)

    def _install_verification_landscape_print(self) -> None:
        """只为查验窗口包装 pywebview 的原生打印入口。"""
        if sys.platform != "darwin":
            return
        try:
            from webview.platforms.cocoa import BrowserView
        except ImportError:
            return

        current = BrowserView.print_webview
        if getattr(current, "_tidoc_landscape_print", False):
            return
        original = current

        def print_webview_landscape(native_webview):
            window = getattr(native_webview, "pywebview_window", None)
            if getattr(window, "title", "") != "tidoc · 发票查验":
                return original(native_webview)
            job_title = getattr(window, "_tidoc_print_title", "查验单")
            return self._run_macos_verification_print(native_webview, job_title)

        print_webview_landscape._tidoc_landscape_print = True
        BrowserView.print_webview = staticmethod(print_webview_landscape)

    @staticmethod
    def _run_macos_verification_print(native_webview, job_title: str) -> None:
        """创建带横向设置和明确任务名的 macOS 原生打印任务。"""
        import AppKit
        import Foundation

        info = AppKit.NSPrintInfo.sharedPrintInfo().copy()
        InvoiceVerificationApi._configure_verification_print_info(info)
        info.setHorizontalPagination_(AppKit.NSFitPagination)
        info.setHorizontallyCentered_(Foundation.NO)
        info.setVerticallyCentered_(Foundation.NO)

        imageable_bounds = info.imageablePageBounds()
        paper_size = info.paperSize()
        if Foundation.NSWidth(imageable_bounds) > paper_size.width:
            imageable_bounds.origin.x = 0
            imageable_bounds.size.width = paper_size.width
        if Foundation.NSHeight(imageable_bounds) > paper_size.height:
            imageable_bounds.origin.y = 0
            imageable_bounds.size.height = paper_size.height

        info.setBottomMargin_(Foundation.NSMinY(imageable_bounds))
        info.setTopMargin_(
            paper_size.height
            - Foundation.NSMinY(imageable_bounds)
            - Foundation.NSHeight(imageable_bounds)
        )
        info.setLeftMargin_(Foundation.NSMinX(imageable_bounds))
        info.setRightMargin_(
            paper_size.width
            - Foundation.NSMinX(imageable_bounds)
            - Foundation.NSWidth(imageable_bounds)
        )

        print_operation = native_webview._printOperationWithPrintInfo_(info)
        print_operation.setJobTitle_(job_title)
        print_operation.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
            native_webview.window(), None, None, None
        )
