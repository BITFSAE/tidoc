"""独立 OCR 识别组件进程入口。

PyInstaller 打包后由核心通过 JSON 文件 IPC 调用，避免把阿里云 SDK 塞进核心包。
输入 payload：
{
  "credentials": {"access_key_id": "...", "access_key_secret": "..."},
  "endpoint": "ocr-api.cn-hangzhou.aliyuncs.com",   # 可省略
  "tasks": [{"entry_id": "...", "file_path": "/abs/path.pdf"}]
}
输出 result.json：
{"ok": true, "data": {"results": [{"entry_id", "ok", "raw_data", "normalized", "error"}]}}
单张失败不中断批次；密钥通过临时文件传递，不出现在命令行参数里。
"""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

# Keep explicit imports: the package exports symbols lazily via __getattr__,
# which PyInstaller cannot reliably discover when building the component.
from .ocr import DEFAULT_ENDPOINT, normalize_invoice_data, recognize_invoice


def main() -> int:
    parser = argparse.ArgumentParser(prog="tidoc_ocr")
    parser.add_argument("--input", help="核心传入的 JSON payload")
    parser.add_argument("--result", help="组件写出的 JSON 结果")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="验证 OCR 组件及阿里云依赖可以正常导入（不联网）",
    )
    args = parser.parse_args()

    if args.self_test:
        import tidoc_ocr

        missing = tidoc_ocr.missing_dependencies()
        print(json.dumps({
            "ok": not missing,
            "component": "tidoc_ocr",
            "version": tidoc_ocr.__version__,
            "missing": missing,
        }, ensure_ascii=False))
        return 0 if not missing else 1
    if not args.input or not args.result:
        parser.error("--input 和 --result 必须同时提供")

    result_path = Path(args.result)
    try:
        payload = json.loads(Path(args.input).read_text("utf-8"))
        credentials = payload.get("credentials") or {}
        access_key_id = str(credentials.get("access_key_id") or "")
        access_key_secret = str(credentials.get("access_key_secret") or "")
        endpoint = str(payload.get("endpoint") or DEFAULT_ENDPOINT)
        if not access_key_id or not access_key_secret:
            raise ValueError("缺少阿里云 AccessKey。")

        results = []
        for task in payload.get("tasks") or []:
            entry_id = str(task.get("entry_id") or "")
            expected_calls = max(1, int(task.get("page_count") or 1))
            try:
                raw = recognize_invoice(
                    task.get("file_path") or "",
                    access_key_id,
                    access_key_secret,
                    endpoint,
                )
                normalized = normalize_invoice_data(raw)
                results.append({
                    "entry_id": entry_id,
                    "ok": True,
                    "raw_data": raw,
                    "normalized": normalized,
                    "api_calls": int(normalized.get("page_count") or expected_calls),
                    "error": "",
                })
            except Exception as exc:  # noqa: BLE001 — 单张失败不阻断批次
                api_calls = getattr(exc, "api_calls", None)
                results.append({
                    "entry_id": entry_id,
                    "ok": False,
                    "raw_data": None,
                    "normalized": None,
                    "api_calls": expected_calls if api_calls is None else int(api_calls),
                    "error": str(exc),
                })
        _write_result(result_path, {"ok": True, "data": {"results": results}})
        return 0
    except Exception as exc:  # noqa: BLE001
        _write_result(result_path, {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
        return 1


def _write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), "utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
