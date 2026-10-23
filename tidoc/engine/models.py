"""解析结果的数据结构。这里只描述"从一张发票里识别出来的东西"，
不含数据库 / 附件仓库概念——那些在 db 层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# 校验状态（对应设计文档第 5 节 check_status）
CHECK_PASS = "pass"
CHECK_WARNING = "warning"
CHECK_BLOCKED = "blocked"

# 同一字段在不同读取方式（普通文本 / 版面文本）下的比对结论。只有这四种，没有概率。
FIELD_AGREE = "agree"        # 读到的方式结果相同
FIELD_CONFLICT = "conflict"  # 至少两种方式都读到了，但结果不同
FIELD_MISSING = "missing"    # 没读全：全部缺失，或只有一种方式读到而无法比对
FIELD_VERIFIED = "verified"  # 采用的值通过了独立校验（校验位、闭合、大写金额、已配置抬头、合法日期）

# 总额的来源，从可信到不可信
TOTAL_SOURCE_XML = "xml"              # 官方电子发票 XML 的结构化字段
TOTAL_SOURCE_LABEL = "label"          # 票面「价税合计（小写）」
TOTAL_SOURCE_PAIR = "pair"            # 「大写金额 + 小写金额」同行出现，没有标签
TOTAL_SOURCE_DERIVED = "derived"      # 没有小写标签，由大写金额或合计行互相印证得出
TOTAL_SOURCE_MAX_AMOUNT = "max_amount"  # 最后兜底：取页面上最大的 ¥ 金额，可信度低

# 校验提醒的强弱。都以「识别提醒」展示，不改变 pass / warning / blocked 的状态。
ISSUE_STRONG = "strong"      # 票面金额或关键字段互相矛盾，需要人工核对
ISSUE_ADVISORY = "advisory"  # 辅助提示，例如税率估算、校验位


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
class FieldCheck:
    """单个字段在多种读取方式下的比对结果。"""

    state: str = FIELD_MISSING
    value: str = ""                  # 最终采用的值
    candidates: dict = field(default_factory=dict)  # 读取方式 -> 候选值，如 {"text": ..., "layout": ...}
    basis: str = ""                  # 采用依据，如 label / checksum / configured_title / words / closure
    flags: tuple = ()                # 附加发现，如 checksum_failed / invalid_date

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "value": self.value,
            "candidates": dict(self.candidates),
            "basis": self.basis,
            "flags": list(self.flags),
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
    # 以下字段由解析器填写，用于独立校验；手工构造的发票可以全部留空，校验层会跳过对应检查。
    seller_tax_id: str = ""
    subtotal: Decimal | None = None          # 不含税金额合计（票面「合计」行）
    tax_total: Decimal | None = None         # 税额合计
    total_in_words: Decimal | None = None    # 大写金额折算出的数值
    total_words_text: str = ""               # 大写金额原文
    total_source: str = ""                   # TOTAL_SOURCE_*；空表示未标注来源
    total_read: Decimal | None = None        # 解析时读到的总额；total 被外部覆盖后据此识别
    tax_rates: list = field(default_factory=list)  # 票面出现的税率（百分数），0 表示免税 / 不征税
    source_warnings: list[str] = field(default_factory=list)
    field_checks: dict = field(default_factory=dict)  # 字段名 -> FieldCheck

    @classmethod
    def from_dict(cls, data: dict) -> ParsedInvoice:
        """Restore persisted parser evidence without losing Decimal precision."""
        if not isinstance(data, dict):
            raise ValueError("发票识别记录必须是对象。")
        values = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        for name in ("invoice_no", "invoice_date", "seller", "buyer_name", "buyer_tax_id",
                     "seller_tax_id", "source", "total_words_text", "total_source"):
            if name in values and not isinstance(values[name], str):
                raise ValueError(f"发票识别记录中的 {name} 必须是文本。")
        if data.get("total_present") is not None and type(data["total_present"]) is not bool:
            raise ValueError("发票总额读取标记无效。")
        for name in ("items", "tax_rates", "source_warnings"):
            if not isinstance(data.get(name, []), list):
                raise ValueError(f"发票识别记录中的 {name} 必须是列表。")
        if any(not isinstance(warning, str) for warning in data.get("source_warnings", [])):
            raise ValueError("发票来源提醒必须是文本。")
        if not isinstance(data.get("field_checks", {}), dict):
            raise ValueError("发票字段核对记录必须是对象。")

        def decimal(value):
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError("发票识别记录中存在无效金额。")
            return number

        for name in ("total", "subtotal", "tax_total", "total_in_words", "total_read"):
            if name in values and values[name] is not None:
                values[name] = decimal(values[name])
        if values.get("total", Decimal(0)) is None:
            raise ValueError("发票识别记录缺少总额。")
        values["tax_rates"] = [decimal(rate) for rate in data.get("tax_rates", [])]
        for check in data.get("field_checks", {}).values():
            if (not isinstance(check, dict) or not isinstance(check.get("candidates", {}), dict)
                    or any(not isinstance(value, str) for value in check.get("candidates", {}).values())
                    or not isinstance(check.get("value", ""), str)
                    or not isinstance(check.get("flags", []), (list, tuple))):
                raise ValueError("发票字段核对记录无效。")
        for item in data.get("items", []):
            if not isinstance(item, dict) or any(not isinstance(item.get(name, ""), str)
                                                for name in ("name", "actual_name", "unit", "spec")):
                raise ValueError("发票识别明细无效。")
        values["field_checks"] = {name: FieldCheck(**check) for name, check in data.get("field_checks", {}).items()}
        values["items"] = [ParsedItem(
            name=item["name"], actual_name=item["actual_name"], unit=item["unit"],
            quantity=decimal(item["quantity"]) if item.get("quantity") not in (None, "") else None,
            total=decimal(item["total"]), spec=item.get("spec", ""),
        ) for item in data.get("items", [])]
        return cls(**values)

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
            "seller_tax_id": self.seller_tax_id,
            "subtotal": None if self.subtotal is None else str(self.subtotal),
            "tax_total": None if self.tax_total is None else str(self.tax_total),
            "total_in_words": None if self.total_in_words is None else str(self.total_in_words),
            "total_words_text": self.total_words_text,
            "total_source": self.total_source,
            "total_read": None if self.total_read is None else str(self.total_read),
            "source_warnings": list(self.source_warnings),
            "tax_rates": [str(rate) for rate in self.tax_rates],
            "field_checks": {name: check.to_dict() for name, check in self.field_checks.items()},
        }


@dataclass(frozen=True)
class CheckIssue:
    """一条新增的校验提醒。message 已并入 CheckResult.message，这里保留编码和强弱供程序使用。"""

    code: str
    level: str   # ISSUE_STRONG / ISSUE_ADVISORY
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass
class CheckResult:
    """金额闭合 / 抬头一致性校验的结论。"""

    status: str = CHECK_WARNING     # pass / warning / blocked
    message: str = ""
    # 票面金额与字段校验新增的提醒（金额闭合、大写金额、字段冲突等）；旧有的明细、抬头提醒只在 message 里。
    issues: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message,
                "issues": [issue.to_dict() for issue in self.issues]}


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
