"""Canonical adapter definition, strict setting resolution and deterministic digests."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .registry import SETTINGS


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _valid_setting(key, value):
    spec = SETTINGS[key]
    kind = spec['type']
    if value is None:
        return spec.get('nullable', False)
    if kind == 'boolean':
        return type(value) is bool
    if kind == 'text':
        return isinstance(value, str) and len(value) <= spec.get('max_length', 10000)
    if kind == 'select':
        return isinstance(value, str) and value in spec.get('enum', [])
    if kind == 'multiselect':
        return isinstance(value, list) and len(value) <= spec.get('max_items', 10000) and all(
            isinstance(item, str) and len(item) <= spec.get('max_length', 10000) for item in value
        )
    if kind in ('integer', 'decimal', 'money'):
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            return False
        if kind == 'money' and not isinstance(value, str):
            return False
        try:
            number = Decimal(str(value))
            if not number.is_finite() or (kind == 'integer' and number != number.to_integral_value()):
                return False
            return (number >= Decimal(str(spec['minimum'])) if 'minimum' in spec else True) and (
                number <= Decimal(str(spec['maximum'])) if 'maximum' in spec else True
            )
        except (InvalidOperation, ValueError):
            return False
    return False


def resolve_definition(definition: dict, overrides: dict | None = None) -> dict:
    """Resolve registered defaults, package policy and local overrides immutably."""
    result = deepcopy(definition)
    for name in ('fields', 'materials', 'rules', 'outputs'):
        result.setdefault(name, [])
    result.setdefault('scheme', {}).setdefault('settings', {})
    overrides = overrides or {}
    unknown = set(overrides) - set(SETTINGS)
    if unknown:
        raise ValueError(f'Unregistered setting override(s): {", ".join(sorted(unknown))}')
    descriptors = result['scheme']['settings']
    import_defaults = result['scheme'].get('import_defaults', {})
    import_keys = {'entry.default_title_id', 'entry.default_paid_to_invoice', 'entry.suggested_tags'}
    unknown_import_defaults = set(import_defaults) - import_keys
    if unknown_import_defaults:
        raise ValueError('Unregistered import default(s): ' + ', '.join(sorted(unknown_import_defaults)))
    for key, value in import_defaults.items():
        if key in descriptors:
            raise ValueError(f'Import default conflicts with scheme setting: {key}')
        if not _valid_setting(key, value):
            raise ValueError(f'Invalid import default: {key}')
    effective = {}
    for key, spec in SETTINGS.items():
        desc = descriptors.get(key, {})
        if 'fixed' in desc:
            if key in overrides and overrides[key] != desc['fixed']:
                raise ValueError(f'Local override conflicts with fixed setting: {key}')
            value = desc['fixed']
        elif key in overrides:
            value = overrides[key]
        elif 'default' in desc:
            value = desc['default']
        elif key in result['scheme'].get('import_defaults', {}):
            value = result['scheme']['import_defaults'][key]
        elif key == 'print.default_outputs':
            value = result['scheme'].get('default_outputs',
                [output['id'] for output in result['outputs'] if output.get('default_selected')])
        else:
            value = spec['default']
        if not _valid_setting(key, value):
            raise ValueError(f'Invalid value for registered setting: {key}')
        effective[key] = deepcopy(value)
    result['effective_settings'] = effective
    return result


def revision_hash(definition: dict) -> str:
    """Hash semantic definition data, excluding local identity and machine paths."""
    stable = deepcopy(definition)
    for key in ('revision_id', 'installed_at', 'absolute_path', 'scheme_id', 'local_id'):
        stable.pop(key, None)
    return hashlib.sha256(canonical_json(stable)).hexdigest()
