"""Lightweight protocol types. No application, database or printer imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

FieldType = Literal['text', 'multiline', 'integer', 'decimal', 'money', 'date', 'boolean', 'select', 'multiselect']
Scope = Literal['scheme', 'payee', 'entry', 'batch', 'export']
Stage = Literal['complete', 'export']
Severity = Literal['info', 'warning', 'required']


class Diagnostic(TypedDict, total=False):
    code: str
    file: str
    location: str
    message: str
    severity: str
    rule_id: str
    stage: str
    target: str
    suggested_action: str
    field: str
    suggestion: str


def diagnostic(code: str, message: str, *, file: str = '', location: str = '/',
               severity: str = 'required', **extra: Any) -> Diagnostic:
    return Diagnostic(code=code, file=file, location=location, message=message,
                      severity=severity, **extra)


class AdapterValidationError(ValueError):
    def __init__(self, diagnostics: list[dict] | str):
        self.diagnostics = ([diagnostic('INVALID_ADAPTER', diagnostics)]
                            if isinstance(diagnostics, str) else diagnostics)
        super().__init__('; '.join(d.get('message', '') for d in self.diagnostics))


class FieldValidationError(AdapterValidationError):
    pass


@dataclass
class AdapterPackage:
    definition: dict
    content_hash: str
    files: dict[str, bytes]
    diagnostics: list[dict] = field(default_factory=list)
    source: str = ''

    @property
    def manifest(self) -> dict:
        return self.definition['manifest']

    @property
    def package_id(self) -> str:
        return self.manifest['package_id']

    @property
    def package_version(self) -> str:
        return self.manifest['package_version']

    @property
    def name(self) -> str:
        return self.manifest['name']


@dataclass(frozen=True)
class FieldDefinition:
    id: str
    scope: Scope
    type: FieldType
    label: str
    definition: dict

    @classmethod
    def from_dict(cls, value: dict) -> FieldDefinition:
        return cls(value['id'], value['scope'], value['type'], value['label'], value)


@dataclass(frozen=True)
class MaterialRole:
    id: str
    label: str
    min_count: int = 0
    max_count: int | None = None


@dataclass(frozen=True)
class SchemeRevision:
    revision_id: str
    definition: dict
