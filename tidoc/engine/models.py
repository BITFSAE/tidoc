"""解析结果的数据结构。这里只描述"从一张发票里识别出来的东西"，
不含数据库 / 附件仓库概念——那些在 db 层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# 校验状态（对应设计文档第 5 节 check_status）
CHECK_PASS = "pass"
CHECK_WARNING = "warning"
CHECK_BLOCKED = "blocked"


@dataclass
class ParsedItem:
    """一条物品明细。"""

    name: str            # 发票原始物资名称（可能带 *分类* 星号）
    actual_name: str     # 去掉分类星号后的实际名称
    unit: str
    quantity: Decimal | None
    total: Decimal       # 该行含税金额
    spec: str = ""

    @property
    def unit_price(self) -> Decimal:
        if self.quantity and self.quantity != 0:
            return self.total / self.quantity
        return self.total

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "actual_name": self.actual_name,
            "unit": self.unit,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "total": str(self.total),
            "spec": self.spec,
        }


@dataclass
class ParsedInvoice:
    """一张发票的识别结果。"""

    invoice_no: str = ""
    invoice_date: str = ""
    seller: str = ""
    buyer_name: str = ""        # 购买方抬头
    buyer_tax_id: str = ""
    total: Decimal = Decimal("0")
    items: list[ParsedItem] = field(default_factory=list)
    source: str = ""           # 数据来源说明（xml / pdf / xml+pdf）
    total_present: bool | None = None  # False means absent; zero is a valid invoice amount.

    def to_dict(self) -> dict:
        return {
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date,
            "seller": self.seller,
            "buyer_name": self.buyer_name,
            "buyer_tax_id": self.buyer_tax_id,
            "total": str(self.total),
            "items": [item.to_dict() for item in self.items],
            "source": self.source,
            "total_present": self.total_present,
        }


@dataclass
class CheckResult:
    """金额闭合 / 抬头一致性校验的结论。"""

    status: str = CHECK_WARNING     # pass / warning / blocked
    message: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message}


@dataclass(frozen=True)
class RecognitionContext:
    """Candidates belong to the selected revision, including an explicitly empty list."""

    titles: tuple[dict, ...] = ()

    @classmethod
    def from_definition(cls, definition: dict):
        return cls(tuple(dict(t) for t in definition.get("scheme", {}).get("titles", [])))

    @property
    def fingerprint(self) -> str:
        import hashlib
        import json
        return hashlib.sha256(json.dumps(self.titles, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyContext(RecognitionContext):
    """Explicit title policy, independent of other windows or API instances."""
