"""解析引擎：发票 XML/PDF 解析、金额闭合校验。移植自 invoice2docx/engine.py。"""

from .models import (
    CHECK_BLOCKED,
    CHECK_PASS,
    CHECK_WARNING,
    CheckResult,
    ParsedInvoice,
    ParsedItem,
)
from .money import d, fmt_decimal, fmt_money, money
from .validator import (
    EXPECTED_BUYER_TAX_IDS,
    SUPPORTED_TITLES,
    TAX_ID_FOUNDATION,
    TAX_ID_UNIVERSITY,
    TITLE_FOUNDATION,
    TITLE_UNIVERSITY,
    check_invoice,
    normalize_tax_id,
)

_PARSER_EXPORTS = {"clean_item_name", "parse_xml", "parse_pdf", "parse_aspose_xml", "parse_invoice_files"}


def __getattr__(name):
    if name in _PARSER_EXPORTS:
        from . import parser

        return getattr(parser, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CHECK_BLOCKED",
    "CHECK_PASS",
    "CHECK_WARNING",
    "CheckResult",
    "ParsedInvoice",
    "ParsedItem",
    "d",
    "money",
    "fmt_money",
    "fmt_decimal",
    "clean_item_name",
    "parse_xml",
    "parse_pdf",
    "parse_aspose_xml",
    "parse_invoice_files",
    "check_invoice",
    "SUPPORTED_TITLES",
    "EXPECTED_BUYER_TAX_IDS",
    "TITLE_UNIVERSITY",
    "TITLE_FOUNDATION",
    "TAX_ID_UNIVERSITY",
    "TAX_ID_FOUNDATION",
    "normalize_tax_id",
]
