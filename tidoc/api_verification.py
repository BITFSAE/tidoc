"""User-triggered tax website verification."""
from __future__ import annotations
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from send2trash import send2trash
from .api_shared import InspectionConflictError, _guard, _is_inside, activate_main_window


# 轮询约每 1.2 秒一次，同一份文件连续读取失败这么多次后不再等待。
VERIFICATION_RETRY_LIMIT = 10


class InvoiceVerificationApi:
    def _add_inspection_attachment(self, entry_id, path, note="", options=None, role_id=None):
        """所有新增查验单共用确认规则；替换复用附件仓库的原子更新。"""
        from .db import TYPE_INSPECTION

        options = options or {}
        existing = [att for att in self.attachments.list(entry_id) if att["type"] == TYPE_INSPECTION]
        if existing:
            action = options.get("inspection_action")
            expected = options.get("inspection_existing_ids")
            if (action not in ("replace", "keep") or not isinstance(expected, list)
                    or sorted(str(value) for value in expected) != sorted(att["id"] for att in existing)):
                raise InspectionConflictError(entry_id, existing)
            if action == "replace":
                target = options.get("inspection_replace_id")
                if target not in {att["id"] for att in existing}:
                    raise InspectionConflictError(entry_id, existing)
                return self.attachments.update(target, src_path=path, note=note or None)
        return self.attachments.add(entry_id, path, TYPE_INSPECTION, note, role_id=role_id)

    @_guard
    def invoice_verification_info(self, entry_id):
        """从已有发票材料准备官网查验表单，但不主动联网。"""
        from .services.invoice_verification import build_verification_info

        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")
        return {**build_verification_info(entry), "auto_save": sys.platform in ("darwin", "win32")}

    @_guard
    def start_invoice_verification(self, entry_id, fields=None):
        """用户主动打开税务官网，预填发票信息并创建结果保存会话。"""
        import webview

        from .services.invoice_verification import (
            TAX_VERIFICATION_URL,
            build_verification_info,
            make_prefill_script,
            make_auto_save_script,
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
        auto_save = sys.platform in ("darwin", "win32")
        snapshot = new_session_snapshot()
        verification_preferences = self._invoice_verification_preferences()
        custom_watch = verification_preferences["watch_directory"]
        if custom_watch:
            custom_watch_path = Path(custom_watch).expanduser()
            if not auto_save and not custom_watch_path.is_dir():
                raise ValueError("设置中的查验单归档目录不存在，请重新选择。")
            custom_watch_path = custom_watch_path.resolve()
            inside_data = _is_inside(self.data_root.root, custom_watch_path)
            if not auto_save and inside_data:
                raise ValueError("查验单归档目录不能位于 tidoc 数据目录内。")
            if (custom_watch_path.is_dir() and not inside_data
                    and custom_watch_path not in snapshot["watch_directories"]):
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
        if auto_save:
            session["auto_save_dir"] = tempfile.TemporaryDirectory(prefix="tidoc-verification-")
            session["auto_save_path"] = (
                Path(session["auto_save_dir"].name)
                / f"{verification_print_title(info['invoice_no'])}.pdf"
            )
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
            window._tidoc_verification_session = session  # type: ignore[attr-defined]
            window._tidoc_save_pdf = lambda browser: self._save_windows_verification_pdf(  # type: ignore[attr-defined]
                browser, session
            )
            session["window"] = window

            def on_window_closed() -> None:
                with self._api_lock:
                    session["window_closed"] = True

            window.events.closed += on_window_closed
            script = make_prefill_script(info, auto_save=auto_save)
            print_compatibility_script = make_print_compatibility_script(
                info["invoice_no"],
                print_message=(["_tidocVerificationPrint", session_id]
                               if sys.platform == "win32" else None),
            )
            if auto_save:
                print_compatibility_script += make_auto_save_script(info["invoice_no"])

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
            self._cleanup_verification_temp(session)
            raise

        return {
            "session_id": session_id,
            "watch_directories": [str(path) for path in session["watch_directories"]],
            "info": info,
            "auto_save": auto_save,
        }

    @_guard
    def invoice_verification_status(self, session_id, options=None):
        """检测用户刚保存的官网查验单，确认归属后直接绑定到当前条目。"""
        from .db import TYPE_INSPECTION
        from .services.folder_import import (
            classify_pdf_attachment_type,
            extract_pdf_invoice_no,
        )
        from .services.invoice_verification import changed_pdf_candidates
        from .services.inspection_pdf import read_inspection_pdf

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
        auto_path = session.get("auto_save_path")
        if auto_path and auto_path.is_file() and not session.get("auto_save_printing"):
            candidates.insert(0, auto_path)
        waiting_for_write = False
        for path in candidates:
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            size = stat.st_size
            version = (key, stat.st_mtime_ns, size)
            if version in session["seen_candidates"]:
                continue
            previous_size = session["candidate_sizes"].get(key)
            session["candidate_sizes"][key] = size
            versions = session.setdefault("candidate_versions", {})
            previous_version = versions.get(key, version)
            versions[key] = version
            if previous_size != size or previous_version != version or size == 0:
                waiting_for_write = True
                continue
            processed = True
            try:
                # Windows 打印程序可能在大小稳定后仍独占文件，留待下一轮重试。
                with path.open("rb") as source:
                    source.read(1)
                automatic = path == auto_path
                if classify_pdf_attachment_type(path) != TYPE_INSPECTION:
                    if automatic:
                        session["auto_save_rejected"] = True
                        session["last_message"] = "自动生成的 PDF 无法确认为查验单，请点击官网“打印”另存 PDF，或上传已有查验单。"
                    continue
                # 自动输出的标题由程序设置，不能把标题里的号码当成正文确认。
                detected_invoice_no = (
                    read_inspection_pdf(path).get("invoice_no", "")
                    or ("" if automatic else extract_pdf_invoice_no(path))
                )
                if (detected_invoice_no and session["invoice_no"]
                        and detected_invoice_no != session["invoice_no"]):
                    session["last_message"] = (
                        f"发现一份其他发票的查验单（{detected_invoice_no}），未绑定。"
                    )
                    if automatic:
                        session["auto_save_rejected"] = True
                        session["last_message"] += "请点击官网“打印”另存 PDF，或上传已有查验单。"
                    continue
                if session["invoice_no"] and not detected_invoice_no:
                    if automatic:
                        session["auto_save_rejected"] = True
                    session["last_message"] = (
                        "自动生成的查验单正文未能确认发票号码，请点击官网“打印”另存 PDF，或上传已有查验单。"
                        if automatic else
                        "发现一份查验单，但未能确认发票号码，未自动绑定。"
                        "原 PDF 已保留，请手动选择这份文件。"
                    )
                    continue
                self._validate_attachment_for_entry(
                    session["entry_id"], path, TYPE_INSPECTION
                )
                latest = path.stat()
                if (latest.st_mtime_ns, latest.st_size) != (stat.st_mtime_ns, size):
                    processed = False
                    waiting_for_write = True
                    continue
                attachment = self._add_inspection_attachment(
                    session["entry_id"], path,
                    note=("由全国增值税发票查验平台自动保存" if automatic
                          else "由全国增值税发票查验平台原生打印保存"),
                    options=options,
                )
                self.entries.recompute_status(session["entry_id"])
                session["attached"] = attachment
                result_message = "查验单已保存到当前条目。"
                cleanup_warning = ""
                source_trashed = False
                if session.get("trash_source_after_archive") and not automatic:
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
                self._cleanup_verification_temp(session)
                return {
                    "state": "attached",
                    "attachment": attachment,
                    "message": result_message,
                    "cleanup_warning": cleanup_warning,
                    "source_trashed": source_trashed,
                }
            except InspectionConflictError:
                processed = False
                # 确认框在主窗口里；查验窗口盖在上面（macOS 还可能并成标签页）时用户看不到，
                # 查验会像卡住一样没有结果。
                if self._window is not None:
                    activate_main_window(self._window)
                raise
            except Exception as exc:  # noqa: BLE001 - 继续等待用户保存正确文件
                retries = session.setdefault("retry_counts", {})
                # 文件被打印程序占用通常几秒内释放；磁盘已满、目录不可写等持续性错误
                # 不能无限显示“处理中”，超过次数后当作绑定失败并给出原因。
                if isinstance(exc, OSError) and retries.get(version, 0) < VERIFICATION_RETRY_LIMIT:
                    retries[version] = retries.get(version, 0) + 1
                    processed = False
                    waiting_for_write = True
                    session["last_message"] = f"查验单暂时无法读取，正在等待文件保存完成：{exc}"
                else:
                    session["last_message"] = f"发现 PDF，但未能绑定：{exc}"
            finally:
                if processed:
                    session["seen_candidates"].add(version)

        state = "processing" if waiting_for_write or session.get("auto_save_printing") else (
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
            self._cleanup_verification_temp(session)
        return {"closed": bool(session)}

    @staticmethod
    def _cleanup_verification_temp(session: dict) -> None:
        if session.get("auto_save_printing"):
            session["cleanup_pending"] = True
            return
        directory = session.pop("auto_save_dir", None)
        if directory:
            directory.cleanup()

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
        """查验单以横向分页；缩放由页面内容完成，见 make_print_compatibility_script。"""
        if sys.platform != "darwin":
            return
        try:
            import AppKit
        except ImportError:
            return

        info.setOrientation_(AppKit.NSPaperOrientationLandscape)

    def _install_verification_landscape_print(self) -> None:
        """只为查验窗口包装 pywebview 的原生打印入口。"""
        if sys.platform == "win32":
            from .services.verification_windows import install_pdf_print

            install_pdf_print()
            return
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
            session = getattr(window, "_tidoc_verification_session", {})
            output = session.get("auto_save_path")
            if output and not session.get("window_closed") and not session.get("auto_save_rejected"):
                if session.get("auto_save_printing") or output.exists():
                    return
                session["auto_save_printing"] = True

                def completed(success):
                    session["auto_save_printing"] = False
                    session.pop("auto_save_delegate", None)
                    if not success:
                        output.unlink(missing_ok=True)
                        session["last_message"] = "自动保存查验单失败，请点击官网“打印”重试。"
                    if session.pop("cleanup_pending", False):
                        self._cleanup_verification_temp(session)

                try:
                    session["auto_save_delegate"] = self._run_macos_verification_print(
                        native_webview, job_title, output, completed
                    )
                except Exception as exc:  # noqa: BLE001 - 保留官网手动重试入口
                    completed(False)
                    session["last_message"] = f"自动保存查验单失败，请点击官网“打印”重试：{exc}"
                return
            return self._run_macos_verification_print(native_webview, job_title)

        print_webview_landscape._tidoc_landscape_print = True
        BrowserView.print_webview = staticmethod(print_webview_landscape)

    def _save_windows_verification_pdf(self, browser, session: dict) -> None:
        """原生消息回调已在 UI 线程；只允许写入此会话的私有临时路径。"""
        output = session.get("auto_save_path")
        if not output or session.get("auto_save_rejected"):
            browser.webview.CoreWebView2.ShowPrintUI()
            return
        if session.get("window_closed") or session.get("auto_save_printing") or output.exists():
            return
        session["auto_save_printing"] = True
        session["last_message"] = ""

        def completed(success):
            session["auto_save_printing"] = False
            if not success:
                output.unlink(missing_ok=True)
                session["auto_save_rejected"] = True
                session["last_message"] = "自动保存查验单失败，请点击官网“打印”另存 PDF，或上传已有查验单。"
            if session.pop("cleanup_pending", False):
                self._cleanup_verification_temp(session)

        try:
            from .services.verification_windows import run_pdf_print

            run_pdf_print(browser, output, completed)
        except Exception:  # noqa: BLE001 - 原生运行时异常保留系统打印入口
            completed(False)

    @staticmethod
    def _run_macos_verification_print(
        native_webview, job_title: str, output: Path | None = None, completed=None
    ):
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

        if output is not None:
            settings = info.dictionary()
            settings[AppKit.NSPrintJobDisposition] = AppKit.NSPrintSaveJob
            settings[AppKit.NSPrintJobSavingURL] = Foundation.NSURL.fileURLWithPath_(str(output))
        print_operation = native_webview._printOperationWithPrintInfo_(info)
        print_operation.setJobTitle_(job_title)
        if output is not None:
            print_operation.setShowsPrintPanel_(False)
            print_operation.setShowsProgressPanel_(False)
            from .services.verification_macos import run_pdf_print

            return run_pdf_print(print_operation, native_webview.window(), completed)
        print_operation.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
            native_webview.window(), None, None, None
        )
