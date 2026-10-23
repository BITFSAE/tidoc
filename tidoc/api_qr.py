"""User-triggered QR ticket fetch and cancellation."""
from __future__ import annotations
import uuid
from pathlib import Path
from .api_shared import _remove_empty_dropped_parent


class QrTicketApi:
    def fetch_invoice_by_qr(self, image_path, options=None):
        """二维码取票：识别图片里的税务平台取票二维码，联网取回发票 XML / PDF，暂存到拖入中转区。

        只在用户把二维码图片交给软件时联网，访问二维码里的税务平台链接，不访问其他地址。
        成功返回 {paths, files, host, invoice_no, warnings}；paths 可直接交给 scan_files /
        批量导入。失败返回 {ok:false, error, code, stage, open_url?}，open_url 是已通过校验的取票链接，
        可交给 open_external_url 让用户在浏览器里下载后手工导入。options.preview_only 只识别、不联网。
        联网可能持续数十秒到几分钟（慢平台生成 PDF），因此不走 _guard，不占用全局调用锁。
        """
        from .services import qr_fetch

        link = None
        request_id = str((options or {}).get("request_id") or uuid.uuid4().hex)
        cancellation = qr_fetch.FetchCancellation()
        with self._qr_fetch_lock:
            self._qr_fetch_jobs[request_id] = cancellation
        try:
            retry_url = (options or {}).get("retry_url")
            if retry_url:
                link = qr_fetch.parse_invoice_link(str(retry_url))
            else:
                data = qr_fetch.read_image_file(image_path)
                link = qr_fetch.find_invoice_link(data)
            cancellation.check()
            if (options or {}).get("preview_only"):
                return {"ok": True, "data": {"preview_only": True, "host": link.host, "invoice_no": link.invoice_no}}
            result = qr_fetch.fetch_invoice_files(link, cancellation)
            cancellation.check()
            self.data_root.dropped_dir.mkdir(parents=True, exist_ok=True)
            files = []
            try:
                for item in result.files:
                    dest = self._staged_drop_path(item.filename)
                    files.append({"kind": item.kind, "name": item.filename, "path": str(dest), "size": len(item.data)})
                    cancellation.check()
                    dest.write_bytes(item.data)
                cancellation.check()
            except BaseException:
                for item in files:
                    Path(item["path"]).unlink(missing_ok=True)
                    _remove_empty_dropped_parent(self.data_root.dropped_dir, Path(item["path"]).parent)
                raise
            return {"ok": True, "data": {
                "host": result.host,
                "invoice_no": result.invoice_no,
                "paths": [item["path"] for item in files],
                "files": files,
                "warnings": result.warnings,
            }}
        except qr_fetch.QrFetchError as exc:
            error = {"ok": False, "error": str(exc), "code": exc.code, "stage": exc.stage, "detail": exc.detail}
            if link is not None and exc.code in qr_fetch.BROWSER_FALLBACK_CODES:
                error["open_url"] = link.delivery_url
            return error
        except Exception as exc:  # noqa: BLE001 — 桥不能抛，统一转错误
            return {"ok": False, "error": str(exc)}
        finally:
            with self._qr_fetch_lock:
                self._qr_fetch_jobs.pop(request_id, None)

    def cancel_invoice_qr(self, request_id):
        with self._qr_fetch_lock:
            cancellation = self._qr_fetch_jobs.get(str(request_id))
        if cancellation:
            cancellation.cancel()
        return {"ok": True, "data": {"cancelled": bool(cancellation)}}
