"""Derive current manual differences without erasing audit history."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from ..engine.money import money

ITEM_FIELDS = ("name", "actual_name", "unit", "quantity", "unit_price", "total", "spec")


def _text(value):
    return str(value if value is not None else "").strip()


def _number(value, *, amount=False):
    text = _text(value)
    if not text:
        return text
    try:
        number = Decimal(text.replace(",", ""))
        if number.is_finite():
            number = money(number) if amount else number
            return format(number.normalize(), "f") if number else "0"
    except (InvalidOperation, ValueError):
        pass
    return text


def same_value(field, left, right):
    if field in {"total", "paid_amount"}:
        return _number(left, amount=True) == _number(right, amount=True)
    return _text(left) == _text(right)


def item_values(items):
    """Compare content, preserving row order while ignoring database row ids."""
    result = []
    for item in items:
        values = {field: _text(item.get(field)) for field in ITEM_FIELDS}
        if "unit_price" not in item:
            try:
                quantity = Decimal(values["quantity"] or "0")
                total = Decimal(values["total"] or "0")
                values["unit_price"] = str(money(total / quantity if quantity else total))
            except (InvalidOperation, ValueError, ZeroDivisionError):
                pass
        for field in ("quantity", "unit_price", "total"):
            values[field] = _number(values[field], amount=field != "quantity")
        result.append(values)
    return result


def item_snapshot(items):
    return json.dumps(item_values(items), ensure_ascii=False)


def _json(value, expected):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, expected):
            return None
        if expected is list and not all(isinstance(item, dict) for item in decoded):
            return None
        return decoded
    except (ValueError, TypeError):
        return None


def current_manual_fields(entry, candidates):
    histories = {}
    for row in entry.get("history", []):
        match = re.fullmatch(r"\[([^]]+)\](.+)", row["field"])
        source, field = match.groups() if match else ("", row["field"])
        histories.setdefault(field, []).append((source, row))

    software = []
    for value in [entry.get("recognition_data"), *entry.get("software_results", [])]:
        parsed = _json(value, dict)
        if parsed is not None:
            software.append(parsed)

    result = []
    for field in candidates:
        events = histories.get(field, [])
        # A later recognition update owns the current value, even if an old
        # permanent flag or manual history remains in the imported data.
        if events and events[-1][0] not in {"", "人工修正"}:
            continue
        if field == "items":
            current = item_values(entry.get("items", []))
            baselines = [items for parsed in software if (items := _json(parsed.get("items"), list)) is not None]
            if events:
                original = _json(events[0][1]["old_value"], list)
                if original is not None:
                    baselines.append(original)
                for source, event in reversed(events):
                    if source not in {"", "人工修正"}:
                        recognized = _json(event["new_value"], list)
                        if recognized is not None:
                            baselines.append(recognized)
                        break
            if any(current == item_values(baseline) for baseline in baselines):
                continue
        else:
            row = entry.get("fields", {}).get(field)
            if row and row.get("value_source") in {"payment_ai", "payment_ocr"}:
                continue
            current = row["current"] if row else entry.get(field)
            baselines = [row["origin"]] if row else []
            if events:
                baselines.append(events[0][1]["old_value"])
                for source, event in reversed(events):
                    if source not in {"", "人工修正"}:
                        baselines.append(event["new_value"])
                        break
            for parsed in software:
                if field == "actual_item_name" and (items := _json(parsed.get("items"), list)) is not None:
                    baselines.append(next(iter(items), {}).get("actual_name", ""))
                else:
                    key = "buyer_name" if field == "title" else field
                    if key in parsed and not (key == "total" and parsed.get("total_present") is False):
                        baselines.append(parsed[key])
            if field == "paid_amount":
                baselines.append(entry.get("total"))
                payments = entry.get("recognized_payments", [])
                if payments:
                    try:
                        baselines.append(str(sum((Decimal(value) for value in payments), Decimal("0"))))
                    except (InvalidOperation, ValueError):
                        pass
            if any(same_value(field, current, baseline) for baseline in baselines):
                continue
        result.append(field)
    return sorted(result)
