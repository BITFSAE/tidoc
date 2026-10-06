"""Core-facing facade for the lightweight, database-free output data contract.

The optional component and core use these same pure functions. Importing this
module does not load any printing dependency or core API/database object.
"""
from tidoc_print.context import (
    assert_pure_context, CONTEXT_VERSION, FILENAME_FIELDS, ROLE_LABELS, ROLE_TYPES,
    build_export_context, decimal_text, decimal_value, digest_data, exact_sum,
    extension_defaults, field_current, filename_for, format_filename, get_path,
    json_data, project_entry, rows_for_entries, safe_name, title_for_entry, title_key,
)

__all__ = [
    'assert_pure_context','CONTEXT_VERSION','FILENAME_FIELDS','ROLE_LABELS','ROLE_TYPES',
    'build_export_context','decimal_text','decimal_value','digest_data','exact_sum',
    'extension_defaults','field_current','filename_for','format_filename','get_path',
    'json_data','project_entry','rows_for_entries','safe_name','title_for_entry','title_key',
]
