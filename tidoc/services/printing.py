"""核心 ↔ 打印导出组件的适配层（设计文档第 9 节）。

打印组件（tidoc_print）是可选安装件，重依赖不进核心。这里：
- 探测组件是否可用。
- 把核心的条目 dict + 附件 + profile 转成组件的 PrintEntry。
- 调组件生成打印件；组件未装时给出清晰提示，不让核心崩。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

from ..db.attachments import (
    TYPE_INSPECTION,
    TYPE_INVOICE_PDF,
    TYPE_PAYMENT,
)
from ..db.entries import EntryRepo
from ..db.profiles import ProfileRepo
from .updater import COMPONENT_PRINT, installed_component_info


def component_status(components_dir: str | Path | None = None) -> dict:
    """打印组件是否可用 + 缺哪些依赖。核心据此决定入口是否置灰。"""
    frozen = bool(getattr(sys, "frozen", False))

    # 从源码启动时优先使用当前工作区的 tidoc_print。否则开发机只要装过
    # 发布版组件，就会悄悄继续调用旧可执行文件，让本地代码修改看似不生效。
    if not frozen:
        try:
            import tidoc_print
        except Exception:  # noqa: BLE001
            tidoc_print = None
        if tidoc_print is not None and tidoc_print.is_available():
            return {
                "available": True,
                "mode": "python",
                "version": getattr(tidoc_print, "__version__", ""),
                "missing": [],
            }

    installed = (
        installed_component_info(components_dir, COMPONENT_PRINT)
        if components_dir
        else {"marker_exists": False, "needs_repair": False, "issue": ""}
    )
    external = Path(installed["executable"]) if installed.get("valid") else None
    if external:
        return {
            "available": True,
            "mode": "external",
            "path": str(external),
            "version": installed.get("version", ""),
            "missing": [],
        }

    # A packaged core must never fall back to the tidoc_print package fragment
    # that PyInstaller may have discovered while analysing this adapter.  The
    # heavy dependencies belong exclusively to the external component.
    if frozen:
        return {
            "available": False,
            "mode": "repair" if installed.get("needs_repair") else "missing",
            "missing": ["打印导出组件"],
            "needs_repair": bool(installed.get("needs_repair")),
            "error": installed.get("issue") or "打印导出组件未安装",
        }

    if tidoc_print is None:
        return {"available": False, "mode": "missing", "missing": ["tidoc_print"]}
    return {"available": False, "mode": "python", "missing": tidoc_print.missing_dependencies()}


def _to_decimal(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal("0")
    except Exception:
        return Decimal("0")


def _entry_to_print(entry: dict, attachments_dir: Path, profile: dict):
    """把核心条目 dict 转成组件的 PrintEntry。"""
    from tidoc_print import PrintEntry, PrintItem

    payload = _entry_to_print_payload(entry, attachments_dir, profile)
    items = [PrintItem(
        actual_name=it["actual_name"],
        product_name=it["product_name"],
        unit=it["unit"],
        quantity=_to_decimal(it["quantity"]) if it["quantity"] else None,
        total=_to_decimal(it["total"]),
        seller=it["seller"],
        invoice_no=it["invoice_no"],
    ) for it in payload["items"]]
    payload["items"] = items
    payload["total"] = _to_decimal(payload["total"])
    return PrintEntry(**payload)


def _entry_to_print_payload(entry: dict, attachments_dir: Path, profile: dict) -> dict:
    """把核心条目 dict 转成外部组件可读的 JSON payload。"""
    def abs_paths(att_type):
        return [str(attachments_dir / a["stored_path"])
                for a in entry.get("attachments", []) if a["type"] == att_type]

    fields = entry.get("fields", {})
    actual_name = (fields.get("actual_item_name", {}).get("current") or "").strip()
    source_items = list(entry.get("items") or [])
    if not source_items:
        invoice_paths = abs_paths(TYPE_INVOICE_PDF)
        if invoice_paths:
            try:
                from ..engine import parse_pdf

                reparsed = parse_pdf(invoice_paths[0])
                if (
                    reparsed.items
                    and (not reparsed.invoice_no or reparsed.invoice_no == entry.get("invoice_no", ""))
                ):
                    source_items = [item.to_dict() for item in reparsed.items]
            except Exception:
                # 历史附件可能损坏或来自不支持的版式；继续使用用户核对过的
                # 条目级名称，不能让打印被一次补识别失败阻断。
                pass
    items = []
    for index, it in enumerate(source_items):
        product_name = it.get("actual_name") or it.get("name", "")
        items.append({
            "actual_name": actual_name if index == 0 and actual_name else product_name,
            "product_name": product_name,
            "unit": it.get("unit") or "个",
            "quantity": it.get("quantity") or "",
            "total": it.get("total") or "0",
            "seller": entry.get("seller", ""),
            "invoice_no": entry.get("invoice_no", ""),
        })
    if not items:
        # PDF 识别不到明细时，条目级“实际物资名称”仍是用户已经核对过的
        # 权威值。打印时带上它，不能退回成没有信息量的“发票物资”。
        fallback_name = actual_name or "未填写品名"
        items.append({
            "actual_name": fallback_name,
            "product_name": fallback_name,
            "unit": "个",
            "quantity": "1",
            "total": entry.get("total") or "0",
            "seller": entry.get("seller", ""),
            "invoice_no": entry.get("invoice_no", ""),
        })
    return {
        "entry_id": entry["id"],
        "title": entry.get("title", ""),
        "invoice_no": entry.get("invoice_no", ""),
        "invoice_date": entry.get("invoice_date", ""),
        "seller": entry.get("seller", ""),
        "total": entry.get("total") or "0",
        "paid_amount": fields.get("paid_amount", {}).get("current", ""),
        "profile_name": profile.get("name", ""),
        "reviewer": profile.get("reviewer", ""),
        "items": items,
        "invoice_pdfs": abs_paths(TYPE_INVOICE_PDF),
        "payment_images": abs_paths(TYPE_PAYMENT),
        "inspection_pdfs": abs_paths(TYPE_INSPECTION),
    }


def build_prints(
    entries_repo: EntryRepo,
    profiles_repo: ProfileRepo,
    attachments_dir: Path,
    entry_ids: list[str],
    out_dir: str | Path,
    options: dict | None = None,
    components_dir: str | Path | None = None,
) -> dict:
    """核心调用入口：生成打印件。返回按抬头分组的结果。"""
    status = component_status(components_dir)
    if not status["available"]:
        raise RuntimeError(
            f"打印导出组件未安装或缺少依赖：{', '.join(status['missing'])}。"
        )

    options = dict(options or {})
    operator_profile = options.pop("operator_profile", {}) or {}
    profiles = {p["id"]: p for p in profiles_repo.list()}
    print_entries = []
    person_profiles: dict[str, dict] = {}
    for eid in entry_ids:
        entry = entries_repo.get(eid)
        if not entry:
            continue
        prof = profiles.get(entry.get("profile_id"), {})
        pe = (
            _entry_to_print_payload(entry, Path(attachments_dir), prof)
            if status.get("mode") == "external"
            else _entry_to_print(entry, Path(attachments_dir), prof)
        )
        print_entries.append(pe)
        entry_key = pe["entry_id"] if isinstance(pe, dict) else pe.entry_id
        print_person = {
            "person_name": operator_profile.get("person_name") or prof.get("name", ""),
            "student_id": operator_profile.get("student_id") or prof.get("student_id", ""),
            "contact": operator_profile.get("contact") or prof.get("contact", ""),
            "bank_name": operator_profile.get("bank_name") or prof.get("bank_name", ""),
            "bank_card": operator_profile.get("bank_card") or prof.get("bank_card", ""),
        }
        person_profiles[entry_key] = print_person

    if not print_entries:
        raise RuntimeError("没有可打印的条目。")

    if status.get("mode") == "external":
        return _build_prints_external(status["path"], print_entries, out_dir, options, person_profiles)

    from tidoc_print import PersonProfile, PrintOptions, build_print_package

    opts = PrintOptions(**(options or {}))
    typed_profiles = {k: PersonProfile(**v) for k, v in person_profiles.items()}
    results = build_print_package(print_entries, out_dir, opts, typed_profiles)
    return {"results": [{"title": r.title, "files": r.files} for r in results]}


def _build_prints_external(executable: str, entries: list, out_dir: str | Path,
                           options: dict | None, profiles: dict) -> dict:
    payload = {
        "entries": [_jsonable(e) for e in entries],
        "out_dir": str(out_dir),
        "options": options or {},
        "profiles": {k: _jsonable(v) for k, v in profiles.items()},
    }
    with tempfile.TemporaryDirectory(prefix="tidoc-print-") as tmp:
        in_path = Path(tmp) / "input.json"
        out_path = Path(tmp) / "result.json"
        in_path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        cmd = [executable, "--input", str(in_path), "--result", str(out_path)]
        if sys.platform == "darwin" and executable.endswith(".app"):
            cmd = ["open", "-W", "-a", executable, "--args", "--input", str(in_path), "--result", str(out_path)]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"打印组件执行失败：{detail or proc.returncode}")
        if not out_path.exists():
            raise RuntimeError("打印组件未返回结果。")
        result = json.loads(out_path.read_text("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "打印组件执行失败。")
        return result["data"]


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
