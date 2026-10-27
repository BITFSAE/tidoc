"""Validate the public output contract without importing the core application."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from itertools import islice
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .context import assert_pure_context, component_resource_path

CONTEXT_SCHEMA = 'team-adapter/1/context.schema.json'


def context_validator():
    return _validators()[0]


def _validators():
    path = component_resource_path(CONTEXT_SCHEMA, package='schemas')
    metadata = path.stat()
    return _read_validator(str(path), metadata.st_mtime_ns, metadata.st_size)


@lru_cache(maxsize=8)
def _read_validator(path, mtime_ns, size):
    """Return the full validator, a shell validator and one validator per array item type.

    Export previews validate several contexts built from the same entries. The shell
    validator skips array items, which are checked one by one and remembered by content,
    so an entry shared by several outputs is validated once.
    """
    schema = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(schema, dict) or schema.get('properties', {}).get('schema_version') != {'const': 1}:
        raise ValueError('打印上下文 Schema 资源无效')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError('打印上下文 Schema 资源无效') from exc
    shell = deepcopy(schema)
    items = {}
    for name, prop in schema.get('properties', {}).items():
        ref = prop.get('items', {}).get('$ref', '') if isinstance(prop.get('items'), dict) else ''
        # Only arrays whose items are a plain $ref and whose own keywords never compare
        # items with each other can be split without changing the result.
        if prop.get('type') == 'array' and set(prop['items']) == {'$ref'} and ref.startswith('#/$defs/') \
                and set(prop) <= {'type', 'items', 'maxItems', 'minItems', 'description'}:
            shell['properties'][name] = {**prop, 'items': True}
            items[name] = Draft202012Validator({'$defs': schema.get('$defs', {}), '$ref': ref})
    return Draft202012Validator(schema), Draft202012Validator(shell), items


_ITEM_RESULTS = {}
_ITEM_RESULTS_LIMIT = 20000


def _item_errors(name, validator, value):
    """Constraint and relative path of each error in one array item, cached by content."""
    key = (name, id(validator), hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).digest())
    cached = _ITEM_RESULTS.get(key)
    if cached is None:
        cached = tuple((error.validator, tuple(error.absolute_path))
                       for error in islice(validator.iter_errors(value), 50))
        if len(_ITEM_RESULTS) >= _ITEM_RESULTS_LIMIT:
            _ITEM_RESULTS.clear()
        _ITEM_RESULTS[key] = cached
    return cached


def _context_errors(context):
    _, shell, items = _validators()
    for error in shell.iter_errors(context):
        yield error.validator, tuple(error.absolute_path)
    if not isinstance(context, dict):
        return
    for name, validator in items.items():
        values = context.get(name)
        if not isinstance(values, list):
            continue  # The shell validator already reported the wrong type.
        for index, value in enumerate(values):
            for keyword, path in _item_errors(name, validator, value):
                yield keyword, (name, index, *path)


def validate_context(context, *, location='/context'):
    assert_pure_context(context)
    diagnostics = []
    for keyword, path in islice(_context_errors(context), 50):
        pointer = ''.join('/' + str(part).replace('~', '~0').replace('/', '~1')
                          for part in path)
        # jsonschema's error message can contain the entire invalid object,
        # including personal data. Report the violated constraint and pointer.
        reason = {
            'type': '数据类型不符', 'required': '缺少必需字段',
            'additionalProperties': '包含未声明字段', 'const': '协议版本不符',
            'enum': '选项不在允许范围内', 'oneOf': '对象结构或类型不符',
            'maxLength': '文字长度超限', 'maxItems': '项目数量超限',
            'maxProperties': '字段数量超限', 'minimum': '数值低于允许范围',
            'maximum': '数值超过允许范围',
        }.get(keyword, '数据不符合公开协议')
        diagnostics.append({'code': 'INVALID_CONTEXT', 'severity': 'blocked',
                            'location': location + pointer, 'message': reason + '。'})
    return diagnostics
