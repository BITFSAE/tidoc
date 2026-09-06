"""核心 ↔ OCR 识别组件的适配层（设计文档第 10 节）。

OCR 组件（tidoc_ocr）是可选安装件，阿里云 SDK 不进核心。这里：
- 探测组件是否可用（与打印组件同构的四态）。
- 通过子进程 JSON IPC 调组件识别发票 PDF（源码模式下直接调用）。
- 计算本地识别与阿里云结果的差异，并按「自动补齐 / 自动修复 / 待确认」
  三档策略落库：XML 是权威数据只补空；PDF 文本来源在金额闭合校验通过时
  允许自动修复；人工改过的值与高风险字段（发票号、总额、抬头相关）只进
  待确认，绝不静默覆盖。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from ..db.attachments import TYPE_INVOICE_PDF, TYPE_INVOICE_XML
from ..db.entries import EntryRepo
from ..db.ocr_results import OcrRepo
from ..engine.money import d, money
from .updater import COMPONENT_OCR, installed_component_info

# 参与比对的发票字段（title 除外：抬头关系分区隔离，不参与 OCR 采用）
COMPARED_FIELDS = ("invoice_no", "invoice_date", "seller", "total", "buyer_name", "buyer_tax_id")
# PDF 文本来源 + 金额闭合 + 未被人工修正时，允许自动覆盖差异的字段
AUTO_FIX_FIELDS = ("invoice_date", "seller")

FIELD_LABELS = {
    "invoice_no": "发票号码",
    "invoice_date": "发票日期",
    "seller": "销售方",
    "total": "价税合计",
    "buyer_name": "购买方抬头",
    "buyer_tax_id": "购买方税号",
    "items": "物品明细",
}


def component_status(components_dir: str | Path | None = None) -> dict:
    """OCR 识别组件是否可用。核心据此决定入口置灰或引导安装。"""
    frozen = bool(getattr(sys, "frozen", False))

    # 源码启动时优先使用当前工作区的 tidoc_ocr（与打印组件一致，避免开发机
    # 悄悄调用旧的可执行文件）。
    if not frozen:
        try:
            import tidoc_ocr
        except Exception:  # noqa: BLE001
            tidoc_ocr = None
        if tidoc_ocr is not None and tidoc_ocr.is_available():
            return {
                "available": True,
                "mode": "python",
                "version": getattr(tidoc_ocr, "__version__", ""),
                "missing": [],
            }

    installed = (
        installed_component_info(components_dir, COMPONENT_OCR)
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

    # 打包后的核心绝不能回退到 PyInstaller 可能扫到的 tidoc_ocr 包碎片。
    if frozen:
        return {
            "available": False,
            "mode": "repair" if installed.get("needs_repair") else "missing",
            "missing": ["OCR 识别组件"],
            "needs_repair": bool(installed.get("needs_repair")),
            "error": installed.get("issue") or "OCR 识别组件未安装",
        }

    if tidoc_ocr is None:
        return {"available": False, "mode": "missing", "missing": ["tidoc_ocr"]}
    return {"available": False, "mode": "python", "missing": tidoc_ocr.missing_dependencies()}


# ---------------------------------------------------------------- 调用组件
def invoke_ocr(
    tasks: list[dict],
    credentials: dict,
    components_dir: str | Path | None = None,
) -> list[dict]:
    """识别一批发票文件。返回逐条 {entry_id, ok, raw_data, normalized, error}。

    单张失败不中断批次；组件级失败（未安装 / 子进程崩溃）抛 RuntimeError。
    """
    status = component_status(components_dir)
    if not status["available"]:
        raise RuntimeError(
            f"OCR 识别组件未安装或缺少依赖：{', '.join(status['missing'])}。"
        )
    if status.get("mode") == "external":
        return _invoke_ocr_external(status["path"], tasks, credentials)

    import tidoc_ocr

    results = []
    for task in tasks:
        try:
            raw = tidoc_ocr.recognize_invoice(
                task["file_path"],
                credentials.get("access_key_id", ""),
                credentials.get("access_key_secret", ""),
            )
            results.append({
                "entry_id": task["entry_id"],
                "ok": True,
                "raw_data": raw,
                "normalized": tidoc_ocr.normalize_invoice_data(raw),
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001 — 单张失败不阻断批次
            results.append({
                "entry_id": task["entry_id"],
                "ok": False,
                "raw_data": None,
                "normalized": None,
                "error": str(exc),
            })
    return results


def _invoke_ocr_external(executable: str, tasks: list[dict], credentials: dict) -> list[dict]:
    payload = {
        "credentials": {
            "access_key_id": credentials.get("access_key_id", ""),
            "access_key_secret": credentials.get("access_key_secret", ""),
        },
        "tasks": tasks,
    }
    with tempfile.TemporaryDirectory(prefix="tidoc-ocr-") as tmp:
        # mkstemp 权限 600：密钥只落在本机临时文件，不进命令行参数
        fd, in_name = tempfile.mkstemp(suffix=".json", dir=tmp)
        in_path = Path(in_name)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        out_path = Path(tmp) / "result.json"
        cmd = [executable, "--input", str(in_path), "--result", str(out_path)]
        if sys.platform == "darwin" and executable.endswith(".app"):
            cmd = ["open", "-W", "-a", executable, "--args", "--input", str(in_path), "--result", str(out_path)]
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OCR 识别组件执行超时。") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"OCR 识别组件执行失败：{detail or proc.returncode}")
        if not out_path.exists():
            raise RuntimeError("OCR 识别组件未返回结果。")
        result = json.loads(out_path.read_text("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "OCR 识别组件执行失败。")
        return result["data"]["results"]


# ---------------------------------------------------------------- 差异比对
def _value_same(field: str, local, ocr) -> bool:
    local_text = str(local or "").strip()
    ocr_text = str(ocr or "").strip()
    if not local_text and not ocr_text:
        return True
    if field == "total":
        if not local_text or not ocr_text:
            return False
        return money(d(local_text)) == money(d(ocr_text))
    return local_text == ocr_text


def _item_tuple(item: dict) -> tuple:
    quantity = str(item.get("quantity") or "").strip()
    total = str(item.get("total") or "").strip()
    # PDF 解析与阿里云经常只差空格/小数点尾零，不能当成不同明细。
    # 规格逐字符去空白（如 FRC1206J205 TS 与 FRC1206J205TS 应当一致）。
    spec = "".join(str(item.get("spec") or "").split())
    return (
        "".join(str(item.get("actual_name") or item.get("name") or "").split()),
        "".join(str(item.get("unit") or "").split()),
        d(quantity) if quantity else None,
        money(d(total)) if total else Decimal("0"),
        spec,
    )


def _items_same(local_items: list[dict], ocr_items: list[dict]) -> bool:
    if len(local_items or []) != len(ocr_items or []):
        return False
    return all(_item_tuple(a) == _item_tuple(b) for a, b in zip(local_items, ocr_items))


def plan_entry_update(
    entry: dict,
    normalized: dict,
    human_modified: set[str] | frozenset = frozenset(),
    *,
    xml_authoritative: bool | None = None,
) -> dict:
    """计算 OCR 结果与条目当前值的差异及处理决策（不落库，可重复计算）。"""
    source = str(entry.get("source") or "")
    # XML 不一定是创建条目时的来源：常见流程是先导 PDF，之后再补官方 XML。
    # 因此以当前是否存在 XML 附件为准；没有附件信息时回退到 source 字段。
    # source 里出现 xml 不一定代表官方电子 XML（aspose-xml 只是 PDF 版面兜底），
    # 只有显式“xml”或“xml+pdf”才视为权威来源；无附件信息时按此回退。
    source_xml = (
        source in {"xml", "xml+pdf"}
        if xml_authoritative is None
        else bool(xml_authoritative)
    )
    closure_pass = bool(normalized.get("closure_pass"))

    field_rows = []
    for field in COMPARED_FIELDS:
        local = str(entry.get(field) or "").strip()
        ocr = str(normalized.get(field) or "").strip()
        if _value_same(field, local, ocr):
            action = "same"
        elif not ocr:
            action = "ocr_empty"
        elif not local:
            # 用户人工改过（即使改成了空值）也不能静默补回，避免覆盖修正意图。
            action = "pending" if field in human_modified else "fill"
        elif (
            field in AUTO_FIX_FIELDS
            and not source_xml
            and closure_pass
            and field not in human_modified
        ):
            action = "autofix"
        else:
            action = "pending"
        field_rows.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "local": local,
            "ocr": ocr,
            "action": action,
        })

    items_differ = not _items_same(entry.get("items") or [], normalized.get("items") or [])
    if not items_differ:
        items_action = "same"
    elif not source_xml and closure_pass:
        items_action = "replace"
    else:
        items_action = "pending"

    pending = [row["field"] for row in field_rows if row["action"] == "pending"]
    if items_action == "pending":
        pending.append("items")

    return {
        "source_xml": source_xml,
        "closure_pass": closure_pass,
        "field_rows": field_rows,
        "items_differ": items_differ,
        "items_action": items_action,
        "pending": pending,
    }


# ---------------------------------------------------------------- 应用策略
def _ocr_items_to_parsed(normalized: dict):
    from ..engine.models import ParsedItem

    items = []
    for item in normalized.get("items") or []:
        quantity = str(item.get("quantity") or "").strip()
        items.append(ParsedItem(
            name=item.get("name") or item.get("actual_name") or "",
            actual_name=item.get("actual_name") or item.get("name") or "",
            unit=item.get("unit") or "",
            quantity=d(quantity) if quantity else None,
            total=d(item.get("total")),
            spec=item.get("spec") or "",
        ))
    return items


def refresh_entry_check(entries_repo: EntryRepo, entry_id: str) -> None:
    """关键值变化后重算校验状态（明细合计、抬头与购买方税号）。"""
    from ..engine import check_invoice
    from ..engine.models import ParsedInvoice

    entry = entries_repo.get(entry_id)
    if not entry:
        return
    parsed = ParsedInvoice(
        invoice_no=entry.get("invoice_no") or "",
        buyer_name=entry.get("buyer_name") or "",
        buyer_tax_id=entry.get("buyer_tax_id") or "",
        total=d(entry.get("total")),
        items=_entry_items_to_parsed(entry.get("items") or []),
    )
    check = check_invoice(parsed, expected_title=entry.get("title") or "")
    entries_repo.set_check(entry_id, check.status, check.message)
    entries_repo.recompute_status(entry_id)


def _entry_items_to_parsed(items: list[dict]):
    from ..engine.models import ParsedItem

    parsed = []
    for item in items:
        quantity = str(item.get("quantity") or "").strip()
        parsed.append(ParsedItem(
            name=item.get("name") or "",
            actual_name=item.get("actual_name") or item.get("name") or "",
            unit=item.get("unit") or "",
            quantity=d(quantity) if quantity else None,
            total=d(item.get("total")),
            spec=item.get("spec") or "",
        ))
    return parsed


def apply_plan(entries_repo: EntryRepo, entry_id: str, plan: dict, normalized: dict) -> dict:
    """执行自动补齐与自动修复；返回实际改动，供汇总展示。"""
    applied_fields = []
    for row in plan["field_rows"]:
        if row["action"] in ("fill", "autofix"):
            entries_repo.ocr_update_locked_field(entry_id, row["field"], row["ocr"])
            applied_fields.append(row["field"])

    items_replaced = False
    if plan["items_action"] == "replace":
        _replace_items(entries_repo, entry_id, normalized)
        items_replaced = True

    # 关键信息变化会改变校验结论（抬头、总额），不能只在 total 变化时刷新。
    if applied_fields or items_replaced:
        refresh_entry_check(entries_repo, entry_id)

    return {"applied_fields": applied_fields, "items_replaced": items_replaced}


def _replace_items(entries_repo: EntryRepo, entry_id: str, normalized: dict) -> None:
    from ..engine import check_invoice
    from ..engine.models import ParsedInvoice

    entry = entries_repo.get(entry_id)
    if not entry:
        raise ValueError("条目不存在。")
    parsed_items = _ocr_items_to_parsed(normalized)
    parsed = ParsedInvoice(
        invoice_no=entry.get("invoice_no") or "",
        buyer_name=entry.get("buyer_name") or "",
        buyer_tax_id=entry.get("buyer_tax_id") or "",
        total=d(entry.get("total")),  # 条目总额是权威值，明细向总额对齐
        items=parsed_items,
    )
    check = check_invoice(parsed, expected_title=entry.get("title") or "")
    entries_repo.replace_recognized_items(
        # OCR 只替换明细，不改变条目识别来源；否则后续再识别会误以为
        # XML 权威信息已被覆盖，导致 XML 条目被自动替换。
        entry_id, parsed_items, entry.get("source") or "", check.status, check.message
    )


# ---------------------------------------------------------------- 编排
def run_ocr_for_entries(
    entries_repo: EntryRepo,
    ocr_repo: OcrRepo,
    attachments_dir: str | Path,
    entry_ids: list[str],
    credentials: dict,
    components_dir: str | Path | None = None,
    include_xml: bool = False,
) -> dict:
    """对一批条目调用阿里云 OCR：识别 → 落库 → 按策略应用 → 返回逐条汇总。"""
    results: list[dict] = []
    skipped: list[dict] = []
    tasks: list[dict] = []
    prepared: list[dict] = []
    seen: set[str] = set()
    for entry_id in entry_ids or []:
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        entry = entries_repo.get(entry_id)
        if not entry:
            skipped.append({"entry_id": entry_id, "reason": "条目不存在"})
            continue
        attachments = entry.get("attachments") or []
        pdf = next((a for a in attachments if a["type"] == TYPE_INVOICE_PDF), None)
        if not pdf:
            skipped.append({"entry_id": entry_id, "reason": "缺少发票 PDF，无法识别"})
            continue
        has_xml = any(a["type"] == TYPE_INVOICE_XML for a in attachments)
        if has_xml and not include_xml:
            skipped.append({"entry_id": entry_id, "reason": "已有 XML 权威数据，默认跳过"})
            continue
        tasks.append({"entry_id": entry_id, "file_path": str(Path(attachments_dir) / pdf["stored_path"])})
        prepared.append({"entry_id": entry_id, "attachment": pdf, "has_xml": has_xml, "title": entry.get("title", "")})

    if not tasks:
        return {"called": 0, "results": results, "skipped": skipped}

    outcomes = invoke_ocr(tasks, credentials, components_dir)
    for task_info, outcome in zip(prepared, outcomes):
        entry_id = task_info["entry_id"]
        attachment = task_info["attachment"]
        if not outcome.get("ok"):
            ocr_repo.record(
                entry_id,
                file_sha256=attachment.get("sha256") or "",
                file_name=attachment.get("original_name") or "",
                status="failed",
                error=str(outcome.get("error") or "识别失败"),
            )
            results.append({
                "entry_id": entry_id,
                "ok": False,
                "error": str(outcome.get("error") or "识别失败"),
            })
            continue

        normalized = outcome["normalized"]
        raw_json = json.dumps(outcome["raw_data"], ensure_ascii=False)
        entry = entries_repo.get(entry_id)
        human_modified = entries_repo.human_modified_locked_fields(entry_id)
        plan = plan_entry_update(
            entry, normalized, human_modified,
            xml_authoritative=bool(task_info.get("has_xml")),
        )
        applied = apply_plan(entries_repo, entry_id, plan, normalized)
        record = ocr_repo.record(
            entry_id,
            file_sha256=attachment.get("sha256") or "",
            file_name=attachment.get("original_name") or "",
            raw_json=raw_json,
            normalized=json.dumps(normalized, ensure_ascii=False),
            closure_pass=bool(normalized.get("closure_pass")),
            pending=plan["pending"],
        )
        ocr_repo.mark_applied(record["id"], plan["pending"])
        results.append({
            "entry_id": entry_id,
            "ok": True,
            "closure_pass": bool(normalized.get("closure_pass")),
            "applied_fields": applied["applied_fields"],
            "items_replaced": applied["items_replaced"],
            "pending": plan["pending"],
            "pending_count": len(plan["pending"]),
        })

    return {"called": len(tasks), "results": results, "skipped": skipped}


def result_view(entries_repo: EntryRepo, ocr_repo: OcrRepo, entry_id: str) -> dict:
    """详情页 OCR 区块的数据：最新结果 + 基于当前条目实时计算的差异与决策。"""
    entry = entries_repo.get(entry_id)
    if not entry:
        raise ValueError("条目不存在。")
    history = ocr_repo.history(entry_id)
    view = {
        "latest": None,
        "latest_failed": ocr_repo.latest_failed(entry_id),
        "history_count": len(history),
        "call_count": ocr_repo.count_calls(),
        "stale": False,
        "plan": None,
    }
    latest = ocr_repo.latest(entry_id)
    if not latest:
        return view
    try:
        normalized = json.loads(latest.get("normalized") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        normalized = {}
    if not normalized:
        return view

    pdf = next(
        (a for a in entry.get("attachments") or [] if a["type"] == TYPE_INVOICE_PDF),
        None,
    )
    attachments = entry.get("attachments") or []
    has_xml = any(a["type"] == TYPE_INVOICE_XML for a in attachments)
    has_invoice_attachment = any(
        a["type"] in {TYPE_INVOICE_PDF, TYPE_INVOICE_XML} for a in attachments
    )
    current_sha = (pdf or {}).get("sha256") or ""
    view["stale"] = ocr_repo.result_is_stale(entry_id, current_sha)
    human_modified = entries_repo.human_modified_locked_fields(entry_id)
    view["latest"] = latest
    view["plan"] = plan_entry_update(
        entry, normalized, human_modified,
        # 有发票附件时按附件判断；旧库没有任何发票附件时退回 source 字段。
        xml_authoritative=has_xml if has_invoice_attachment else None,
    )
    view["ocr_items"] = normalized.get("items") or []
    view["normalized"] = normalized
    return view


def _xml_flag(entry: dict) -> bool | None:
    """判断条目是否以 XML 为权威来源；列表与详情两种条目形态都兼容。

    返回 None 表示没有任何发票附件，回退到 source 字段判断
    （见 plan_entry_update 的回退逻辑）。
    """
    attachments = entry.get("attachments")
    if attachments is not None:
        types = {a["type"] for a in attachments}
        has_xml = TYPE_INVOICE_XML in types
        has_any = bool(types & {TYPE_INVOICE_PDF, TYPE_INVOICE_XML})
    else:
        by_type = entry.get("attachment_types") or {}
        has_xml = bool(by_type.get(TYPE_INVOICE_XML))
        has_any = bool(by_type.get(TYPE_INVOICE_PDF) or by_type.get(TYPE_INVOICE_XML))
    return has_xml if has_any else None


def sync_ocr_states(
    entries_repo: EntryRepo,
    ocr_repo: OcrRepo,
    entries: list[dict],
) -> tuple[set[str], set[str]]:
    """批量重算条目的 OCR 徽标状态（待确认 / 已识别），供列表与详情。

    与逐条 result_view 等价，但只需 3 条批量 SQL：最新成功结果、人工修正
    字段、明细。差异与落库值不一致时才写回，避免列表刷新产生大量 commit。
    解析快照损坏的条目保留原待确认状态，等用户重新识别。
    """
    entries = [entry for entry in entries if entry]
    if not entries:
        return set(), set()
    latest_rows = ocr_repo.latest_ok_rows([entry["id"] for entry in entries])
    if not latest_rows:
        return set(), set()

    human_map = entries_repo.human_modified_locked_fields_map(list(latest_rows))
    items_map: dict[str, list[dict]] | None = None
    pending_ids = {entry_id for entry_id, row in latest_rows.items() if row["pending_list"]}
    recognized_ids = set(latest_rows)

    for entry in entries:
        row = latest_rows.get(entry["id"])
        if row is None:
            continue
        try:
            normalized = json.loads(row.get("normalized") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            normalized = {}
        if not normalized:
            continue
        if "items" not in entry:
            # 列表条目不带明细，取批量快照参与比对；不回写，避免撑大列表载荷
            if items_map is None:
                items_map = entries_repo.items_by_entry(list(latest_rows))
            entry = {**entry, "items": items_map.get(entry["id"], [])}
        plan = plan_entry_update(
            entry, normalized, human_map.get(entry["id"], set()),
            xml_authoritative=_xml_flag(entry),
        )
        pending = list(plan["pending"])
        if pending != row["pending_list"]:
            ocr_repo.mark_applied(row["id"], pending)
        if pending:
            pending_ids.add(entry["id"])
        else:
            pending_ids.discard(entry["id"])
    return pending_ids, recognized_ids


def apply_ocr_field(entries_repo: EntryRepo, ocr_repo: OcrRepo, entry_id: str, field: str) -> dict:
    """用户在详情里采用某个 OCR 字段值。"""
    latest = ocr_repo.latest(entry_id)
    if not latest:
        raise ValueError("该条目还没有阿里云识别结果。")
    if field not in COMPARED_FIELDS:
        raise ValueError(f"字段「{field}」不在阿里云 OCR 采用范围内。")
    try:
        normalized = json.loads(latest.get("normalized") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("已保存的识别结果无法解析，请重新识别。") from exc
    value = str(normalized.get(field) or "").strip()
    if not value:
        raise ValueError(f"阿里云识别结果中没有「{FIELD_LABELS.get(field, field)}」。")

    # 只接受有真实差异的采用；值与当前一致时不必重算、避免白写留痕。
    current = entries_repo.get(entry_id)
    if current and _value_same(field, str(current.get(field) or ""), value):
        view = result_view(entries_repo, ocr_repo, entry_id)
        ocr_repo.mark_applied(latest["id"], view["plan"]["pending"])
        return {"entry": current, "pending": view["plan"]["pending"]}

    entry = entries_repo.ocr_update_locked_field(entry_id, field, value)
    # 采用抬头也会影响校验状态，统一重算，避免卡片状态停留在旧结论。
    refresh_entry_check(entries_repo, entry_id)
    entry = entries_repo.get(entry_id)

    view = result_view(entries_repo, ocr_repo, entry_id)
    ocr_repo.mark_applied(latest["id"], view["plan"]["pending"])
    return {"entry": entry, "pending": view["plan"]["pending"]}


def apply_ocr_items(entries_repo: EntryRepo, ocr_repo: OcrRepo, entry_id: str) -> dict:
    """用户在详情里采用整套阿里云明细。"""
    latest = ocr_repo.latest(entry_id)
    if not latest:
        raise ValueError("该条目还没有阿里云识别结果。")
    try:
        normalized = json.loads(latest.get("normalized") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("已保存的识别结果无法解析，请重新识别。") from exc
    if not normalized.get("items"):
        raise ValueError("阿里云识别结果中没有明细。")

    # 明细已经与当前值一致时也清理待确认状态，避免徽标一直挂着。
    entry = entries_repo.get(entry_id)
    if entry and not _items_same(entry.get("items") or [], normalized.get("items") or []):
        _replace_items(entries_repo, entry_id, normalized)

    view = result_view(entries_repo, ocr_repo, entry_id)
    ocr_repo.mark_applied(latest["id"], view["plan"]["pending"])
    return {"entry": entries_repo.get(entry_id), "pending": view["plan"]["pending"]}
