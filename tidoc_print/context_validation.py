"""Validate the public output contract without importing the core application."""
from __future__ import annotations

import json
from functools import lru_cache
from itertools import islice
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .context import assert_pure_context, component_resource_path

CONTEXT_SCHEMA = 'team-adapter/1/context.schema.json'


def context_validator():
    path = component_resource_path(CONTEXT_SCHEMA, package='schemas')
    metadata = path.stat()
    return _read_validator(str(path), metadata.st_mtime_ns, metadata.st_size)


@lru_cache(maxsize=8)
def _read_validator(path, mtime_ns, size):
    schema = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(schema, dict) or schema.get('properties', {}).get('schema_version') != {'const': 1}:
        raise ValueError('打印上下文 Schema 资源无效')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError('打印上下文 Schema 资源无效') from exc
    return Draft202012Validator(schema)


def validate_context(context, *, location='/context'):
    assert_pure_context(context)
    diagnostics = []
    for error in islice(context_validator().iter_errors(context), 50):
        pointer = ''.join('/' + str(part).replace('~', '~0').replace('/', '~1')
                          for part in error.absolute_path)
        # jsonschema's error message can contain the entire invalid object,
        # including personal data. Report the violated constraint and pointer.
        reason = {
            'type': '数据类型不符', 'required': '缺少必需字段',
            'additionalProperties': '包含未声明字段', 'const': '协议版本不符',
            'enum': '选项不在允许范围内', 'oneOf': '对象结构或类型不符',
            'maxLength': '文字长度超限', 'maxItems': '项目数量超限',
            'maxProperties': '字段数量超限', 'minimum': '数值低于允许范围',
            'maximum': '数值超过允许范围',
        }.get(error.validator, '数据不符合公开协议')
        diagnostics.append({'code': 'INVALID_CONTEXT', 'severity': 'blocked',
                            'location': location + pointer, 'message': reason + '。'})
    return diagnostics
