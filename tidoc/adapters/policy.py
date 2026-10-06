"""Pure typed field normalization and bounded three-valued policy evaluation."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import FieldValidationError, diagnostic
from .registry import FIELD_TYPES, RULE_COMPLETE_FIELDS, RULE_EXPORT_FIELDS, field_catalog

_MISSING = object()
UNKNOWN = None


def validate_field_value(field: dict, value):
    """Validate and normalize an explicit value; None/empty remain explicitly empty.

    Returns a Python value (money/decimal are Decimal), or raises FieldValidationError.
    Defaults are a form initialization concern and are never applied while saving.
    """
    ftype = field.get('type')
    label = field.get('label', field.get('id', '字段'))
    problems = []

    def fail(code, message):
        problems.append(diagnostic(code, message, location=f"/fields/{field.get('id', '')}", target=field.get('id')))

    if ftype not in FIELD_TYPES:
        fail('FIELD_TYPE_UNSUPPORTED', f'{label}使用不支持的字段类型。')
    elif value is None or value == '':
        normalized = value
    elif ftype in ('text', 'multiline', 'select'):
        if not isinstance(value, str):
            fail('FIELD_VALUE_TYPE', f'{label}必须是文本。')
            normalized = value
        else:
            normalized = value
            if ftype == 'select' and value not in {o['value'] for o in field.get('options', [])}:
                fail('FIELD_OPTION_INVALID', f'{label}的选项无效。')
            if 'max_length' in field and len(value) > field['max_length']:
                fail('FIELD_TOO_LONG', f'{label}不能超过 {field["max_length"]} 个字符。')
            if 'min_length' in field and len(value) < field['min_length']:
                fail('FIELD_TOO_SHORT', f'{label}至少需要 {field["min_length"]} 个字符。')
    elif ftype == 'boolean':
        normalized = value
        if type(value) is not bool:
            fail('FIELD_VALUE_TYPE', f'{label}必须是布尔值。')
    elif ftype in ('integer', 'decimal', 'money'):
        normalized = value
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            fail('FIELD_VALUE_TYPE', f'{label}必须是数值。')
        else:
            try:
                number = Decimal(str(value))
                if not number.is_finite():
                    raise InvalidOperation
                if ftype == 'integer':
                    if number != number.to_integral_value():
                        raise InvalidOperation
                    normalized = int(number)
                elif ftype == 'money':
                    if not isinstance(value, str) or not re.fullmatch(r'-?(?:0|[1-9]\d*)(?:\.\d{1,4})?', value):
                        raise InvalidOperation
                    normalized = format(number, 'f')
                else:
                    normalized = format(number, 'f')
                if 'minimum' in field and number < Decimal(str(field['minimum'])):
                    fail('FIELD_BELOW_MINIMUM', f'{label}低于允许的最小值。')
                if 'maximum' in field and number > Decimal(str(field['maximum'])):
                    fail('FIELD_ABOVE_MAXIMUM', f'{label}高于允许的最大值。')
            except (InvalidOperation, ValueError, TypeError):
                fail('FIELD_VALUE_INVALID', f'{label}的数值格式无效。')
    elif ftype == 'date':
        normalized = value
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            fail('FIELD_DATE_INVALID', f'{label}请使用 YYYY-MM-DD 日期。')
    elif ftype == 'multiselect':
        normalized = value
        options = {o['value'] for o in field.get('options', [])}
        if not isinstance(value, list) or any(not isinstance(x, str) or x not in options for x in value):
            fail('FIELD_OPTIONS_INVALID', f'{label}包含无效选项。')
        elif len(value) != len(set(value)):
            fail('FIELD_OPTIONS_DUPLICATE', f'{label}不能重复选择同一选项。')
        else:
            normalized = list(value)
    else:
        normalized = value
    if problems:
        raise FieldValidationError(problems)
    return normalized


def _lookup(context, path):
    cur = context
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _eval(expr, context, allowed, depth=1, counter=None, catalog=None):
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > 8 or counter[0] > 100 or not isinstance(expr, dict):
        return None
    if 'all' in expr:
        vals = [_eval(x, context, allowed, depth + 1, counter, catalog) for x in expr['all']]
        return False if False in vals else None if None in vals else True
    if 'any' in expr:
        vals = [_eval(x, context, allowed, depth + 1, counter, catalog) for x in expr['any']]
        return True if True in vals else None if None in vals else False
    if 'not' in expr:
        value = _eval(expr['not'], context, allowed, depth + 1, counter, catalog)
        return None if value is None else not value
    path, op = expr.get('field'), expr.get('op')
    if path not in allowed:
        return None
    left = _lookup(context, path)
    if op == 'present':
        return left is not _MISSING and left not in (None, '', [])
    if left is _MISSING or left in (None, '', []):
        return None
    right = expr.get('value', _MISSING)
    if right is _MISSING:
        return None
    try:
        kind=(catalog or field_catalog()).get(path)
        if kind in ('integer','decimal','money'):
            left=Decimal(str(left))
            right=[Decimal(str(value)) for value in right] if op=='in' else Decimal(str(right))
            if not left.is_finite() or any(not value.is_finite() for value in (right if op=='in' else [right])):
                return None
        elif kind=='date':
            left=date.fromisoformat(left)
            right=[date.fromisoformat(value) for value in right] if op=='in' else date.fromisoformat(right)
        result = {
            'eq': lambda: left == right, 'ne': lambda: left != right,
            'in': lambda: left in right, 'gt': lambda: left > right,
            'gte': lambda: left >= right, 'lt': lambda: left < right,
            'lte': lambda: left <= right,
        }[op]()
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    return bool(result)


def evaluate_condition(condition: dict, context: dict, stage='complete', definition: dict | None = None) -> bool | None:
    """Evaluate a public rule condition as True, False, or None (undetermined)."""
    allowed = RULE_COMPLETE_FIELDS if stage == 'complete' else RULE_EXPORT_FIELDS
    if stage == 'export':
        allowed = allowed | frozenset(
            key for key in field_catalog(definition or context.get('definition', {}))
            if key.startswith(('entry.fields.', 'batch.fields.', 'payee.fields.', 'export.fields.', 'scheme.fields.'))
        )
    else:
        allowed = allowed | frozenset(
            key for key in field_catalog(definition or context.get('definition', {}))
            if key.startswith('entry.fields.')
        )
    return _eval(condition, context, allowed, catalog=field_catalog(definition or context.get('definition', {})))


def evaluate_policy(definition: dict, context: dict, material_counts: dict, stage='complete') -> list[dict]:
    """Evaluate complete-stage requirements or requirements relevant to this export."""
    results = []
    settings = definition.get('effective_settings', {})
    entry = context.get('entry', {}) if isinstance(context.get('entry'), dict) else {}
    if stage == 'complete' and settings.get('profile.reviewer_required') is True and not entry.get('claimant', {}).get('reviewer'):
        results.append(diagnostic('REVIEWER_REQUIRED', '请填写审核人。', severity='required',
                                  stage=stage, target='profile.reviewer', suggested_action='填写审核人'))

    if stage == 'complete':
        for role in definition.get('materials', []):
            count = material_counts.get(role['id'], 0)
            if count < role.get('min_count', 0):
                results.append(diagnostic('MATERIAL_REQUIRED', f"{role.get('label', role['id'])}还需补充。",
                                          severity='required', stage=stage, target=role['id'],
                                          suggested_action=f"补充{role.get('label', role['id'])}"))
    for role in definition.get('materials', []):
        maximum = role.get('max_count')
        if maximum is not None and material_counts.get(role['id'], 0) > maximum:
            results.append(diagnostic('MATERIAL_MAXIMUM', f"{role.get('label', role['id'])}超过允许份数。",
                                      severity='required', stage=stage, target=role['id'],
                                      suggested_action='移除超出份数的材料'))

    output_id = context.get('export', {}).get('output_id') if isinstance(context.get('export'), dict) else None
    selected_output = next((o for o in definition.get('outputs', []) if o.get('id') == output_id), None)
    needed_fields = set(selected_output.get('required_fields', [])) if selected_output else set()
    for field in definition.get('fields', []):
        required = stage in field.get('required_at', [])
        if not required:
            continue
        if stage == 'complete' and field['scope'] != 'entry':
            continue
        if stage == 'export' and field['scope'] not in ('entry', 'batch', 'payee', 'export', 'scheme'):
            continue
        target = f"{field['scope']}.fields.{field['id']}"
        if stage == 'export':
            # Export requiredness is output-specific. Outputs declare required_fields;
            # an unrelated output must not inherit every export-stage form requirement.
            if selected_output is None or (target not in needed_fields and field['id'] not in needed_fields):
                continue
        visible = evaluate_condition(field['visible_when'], context, stage, definition) if field.get('visible_when') else True
        if visible is False:
            continue
        value = _lookup(context, target)
        if value is _MISSING or value in (None, '', []):
            results.append(diagnostic('FIELD_REQUIRED', f"请填写{field['label']}。", severity='required',
                                      stage=stage, target=target, suggested_action=f"填写{field['label']}"))
        else:
            try:
                validate_field_value(field, value)
            except FieldValidationError as exc:
                for item in exc.diagnostics:
                    item.update(stage=stage, target=target)
                results.extend(exc.diagnostics)

    allowed = RULE_COMPLETE_FIELDS if stage == 'complete' else RULE_EXPORT_FIELDS
    extra_prefixes = ('entry.fields.',) if stage == 'complete' else (
        'entry.fields.', 'batch.fields.', 'payee.fields.', 'export.fields.', 'scheme.fields.'
    )
    allowed = allowed | frozenset(k for k in field_catalog(definition) if k.startswith(extra_prefixes))
    for rule in definition.get('rules', []):
        if rule.get('stage') != stage:
            continue
        verdict = _eval(rule.get('when', {}), context, allowed, catalog=field_catalog(definition))
        if verdict is False:
            continue
        rule_id = rule['id']
        if verdict is None:
            severity = rule.get('severity', 'required')
            results.append(diagnostic('RULE_CONDITION_PENDING', rule.get('message', '规则条件信息不完整。'),
                                      severity='required' if severity == 'required' and rule.get('require') else 'warning',
                                      rule_id=rule_id, stage=stage, target=rule_id,
                                      suggested_action='补充条件字段后重新检查'))
            continue
        for req in rule.get('require', []):
            if req.get('material'):
                role = req['material']
                need = req.get('min_count', 1)
                if material_counts.get(role, 0) < need:
                    results.append(diagnostic('RULE_MATERIAL_REQUIRED', rule.get('message', f'请补充材料：{role}'),
                                              severity=rule.get('severity', 'required'), rule_id=rule_id,
                                              stage=stage, target=role, suggested_action='补充所需材料'))
            elif req.get('field'):
                value = _lookup(context, req['field'])
                if value is _MISSING or value in (None, '', []):
                    results.append(diagnostic('RULE_FIELD_REQUIRED', rule.get('message', '请补充必填信息。'),
                                              severity=rule.get('severity', 'required'), rule_id=rule_id,
                                              stage=stage, target=req['field'], suggested_action='填写所需信息'))
        if not rule.get('require'):
            results.append(diagnostic('RULE_NOTICE', rule['message'], severity=rule.get('severity', 'info'),
                                      rule_id=rule_id, stage=stage, target=rule_id))
    return results
