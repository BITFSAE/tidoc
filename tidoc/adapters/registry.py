"""The adapter API 1 setting, context and resource catalogs."""
from __future__ import annotations

from copy import deepcopy

from tidoc_print.context import context_field_catalog, filename_field_catalog

ADAPTER_API_VERSION = 1
SCHEMA_VERSION = '1.0'
CONTEXT_VERSION = 1
CAPABILITIES = (
    'fields.v1', 'material-roles.v1', 'rules.v1', 'output.docx.v1',
    'output.pdf.v1', 'output.xlsx.v1', 'output.attachments.v1',
)
LIMITS = {
    'compressed_bytes': 25 * 1024 * 1024, 'expanded_bytes': 100 * 1024 * 1024,
    'files': 256, 'json_bytes': 2 * 1024 * 1024, 'fields': 100,
    'custom_materials': 20, 'rules': 100, 'outputs': 30,
    'expression_depth': 8, 'expression_nodes': 100,
    'template_loop_depth': 3, 'render_timeout_seconds': 120,
}
SCOPES = ('scheme', 'payee', 'entry', 'batch', 'export')
FIELD_TYPES = ('text', 'multiline', 'integer', 'decimal', 'money', 'date', 'boolean', 'select', 'multiselect')
COLORS = ('neutral', 'blue', 'green', 'amber', 'purple', 'red', 'teal')
RESERVED_FIELD_IDS = frozenset(('fields', '__class__', '__dict__', 'items', 'invoice_no', 'total', 'paid_amount', 'buyer_name', 'buyer_tax_id'))


def _setting(kind, default, *, enum=None, scopes=('scheme',), **constraints):
    value = {'type': kind, 'default': default, 'scopes': list(scopes), **constraints}
    if enum is not None:
        value['enum'] = enum
    return value


SETTINGS = {
    'entry.default_title_id': _setting('text', None, nullable=True, max_length=64),
    'entry.default_paid_to_invoice': _setting('boolean', True),
    'entry.suggested_tags': _setting('multiselect', [], max_items=30, max_length=80),
    'profile.reviewer_required': _setting('boolean', False),
    'profile.reviewer_presentation': _setting('select', 'visible', enum=['visible', 'advanced', 'hidden']),
    'profile.default_view': _setting('select', 'self', enum=['self', 'delegate']),
    'payee.personnel_number_label': _setting('text', '人员编号', max_length=80),
    'print.default_outputs': _setting('multiselect', [], max_items=30, max_length=64),
    'print.numbering': _setting('boolean', True, scopes=('scheme', 'batch', 'export')),
    'print.image_layout': _setting('select', 'a4_landscape_2', enum=['a4_portrait_1', 'a4_portrait_2', 'a4_portrait_4', 'a4_landscape_1', 'a4_landscape_2', 'a4_landscape_4'], scopes=('scheme', 'batch', 'export')),
    'print.content_order': _setting('select', 'entry', enum=['entry', 'role'], scopes=('scheme', 'batch', 'export')),
    'print.amount_basis': _setting('select', 'invoice', enum=['invoice', 'paid'], scopes=('scheme', 'batch', 'export')),
    'print.payee_mode': _setting('select', 'none', enum=['single', 'by_claimant', 'none'], scopes=('scheme', 'batch', 'export')),
    'print.sort_by': _setting('select', 'selection', enum=['invoice_date', 'invoice_no', 'claimant', 'seller', 'created_at', 'selection'], scopes=('scheme', 'batch', 'export')),
    'assist.cloud_ocr_visible': _setting('boolean', True),
    'assist.verification_visible': _setting('boolean', True),
    'assist.payment_ocr': _setting('select', 'local', enum=['local', 'cloud', 'manual']),
    'transfer.include_notes': _setting('boolean', True),
    'transfer.include_tags': _setting('boolean', True),
}

BUILTIN_ROLES = {
    'invoice': {'id': 'invoice', 'label': '发票', 'extensions': ['.pdf', '.xml'], 'min_count': 1, 'max_count': None, 'order': 0, 'quick_action': True, 'reclassifiable': False, 'presentation': 'visible'},
    'payment_screenshot': {'id': 'payment_screenshot', 'label': '付款截图', 'extensions': ['.png', '.jpg', '.jpeg'], 'min_count': 0, 'max_count': None, 'order': 1, 'quick_action': True, 'reclassifiable': True, 'presentation': 'visible'},
    'physical_image': {'id': 'physical_image', 'label': '实物图', 'extensions': ['.png', '.jpg', '.jpeg'], 'min_count': 0, 'max_count': None, 'order': 2, 'quick_action': False, 'reclassifiable': True, 'presentation': 'visible'},
    'inspection_pdf': {'id': 'inspection_pdf', 'label': '查验单', 'extensions': ['.pdf'], 'min_count': 0, 'max_count': None, 'order': 3, 'quick_action': True, 'reclassifiable': True, 'presentation': 'visible'},
    'other': {'id': 'other', 'label': '其他材料', 'extensions': [], 'min_count': 0, 'max_count': None, 'order': 4, 'quick_action': False, 'reclassifiable': True, 'presentation': 'visible'},
}

# Core and standalone component read the same lightweight public data contract.
FIELD_CATALOG = context_field_catalog()
FILENAME_FIELDS = filename_field_catalog()
RULE_COMPLETE_FIELDS = frozenset(('invoice.total', 'invoice.paid_amount', 'invoice.title_id',
                                  'entry.total', 'entry.paid_amount', 'entry.title_id', 'title.id'))
RULE_EXPORT_FIELDS = RULE_COMPLETE_FIELDS | frozenset(('payee.account_type', 'export.output_id'))


def field_catalog(definition=None):
    if (definition or {}).get('fields'):
        return context_field_catalog(definition)
    return dict(FIELD_CATALOG)


def settings_catalog():
    return deepcopy(SETTINGS)


def required_capabilities(definition):
    result = set()
    if definition.get('fields'):
        result.add('fields.v1')
    materials=definition.get('materials',[])
    material_defaults=all(role == BUILTIN_ROLES.get(role.get('id')) for role in materials)
    if materials and not material_defaults:
        result.add('material-roles.v1')
    if definition.get('rules') or any(f.get('visible_when') for f in definition.get('fields', [])) or any(o.get('when') for o in definition.get('outputs', [])):
        result.add('rules.v1')
    kinds = {'docx': 'output.docx.v1', 'pdf_bundle': 'output.pdf.v1', 'xlsx': 'output.xlsx.v1', 'attachment_zip': 'output.attachments.v1'}
    result.update(kinds[o['type']] for o in definition.get('outputs', []))
    return sorted(result)
