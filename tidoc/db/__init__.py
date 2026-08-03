"""数据层：SQLite + 附件文件仓库。"""

from .attachments import (
    AttachmentRepo,
    TYPE_INSPECTION,
    TYPE_INVOICE_PDF,
    TYPE_INVOICE_XML,
    TYPE_PHYSICAL_IMAGE,
    TYPE_OTHER,
    TYPE_PAYMENT,
)
from .batches import BatchRepo
from .database import Database
from .entries import (
    EDITABLE_FIELDS,
    LOCKED_FIELDS,
    STATUS_COMPLETE,
    STATUS_DRAFT,
    STATUS_PARTIAL,
    EntryRepo,
)
from .paths import DataRoot, default_data_root
from .profiles import ProfileRepo

__all__ = [
    "Database",
    "DataRoot",
    "default_data_root",
    "ProfileRepo",
    "EntryRepo",
    "AttachmentRepo",
    "BatchRepo",
    "EDITABLE_FIELDS",
    "LOCKED_FIELDS",
    "STATUS_DRAFT",
    "STATUS_PARTIAL",
    "STATUS_COMPLETE",
    "TYPE_INVOICE_PDF",
    "TYPE_INVOICE_XML",
    "TYPE_PAYMENT",
    "TYPE_PHYSICAL_IMAGE",
    "TYPE_INSPECTION",
    "TYPE_OTHER",
]
