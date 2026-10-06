"""Lightweight public protocol namespace; implementations load on demand.

Print-side clients can import registry constants without loading jsonschema,
database, API, or renderer code.
"""
from .models import AdapterPackage, AdapterValidationError, FieldValidationError

__all__ = [
    'AdapterPackage', 'AdapterValidationError', 'FieldValidationError',
    'load_package', 'pack_package', 'validate_package',
    'resolve_definition', 'revision_hash', 'evaluate_condition',
    'evaluate_policy', 'validate_field_value',
]

def __getattr__(name):
    if name in {'load_package','pack_package','validate_package'}:
        from . import loader
        return getattr(loader,name)
    if name in {'resolve_definition','revision_hash'}:
        from . import resolver
        return getattr(resolver,name)
    if name in {'evaluate_condition','evaluate_policy','validate_field_value'}:
        from . import policy
        return getattr(policy,name)
    raise AttributeError(name)
