"""Parse a customer case into lookup keys and categorized claims.

The customer message is a lead, not ground truth: IDs found here are only used as
lookup keys for MCP calls, and categories only break ties between detected issues.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .facts import iter_dicts, norm_key, to_datetime, to_money, to_text

ORDER_KEYS = {"orderid", "orderids", "order", "orders", "orderref", "orderreference",
              "orderreferences", "relatedorderid", "relatedorderids"}
LOOKUP_KEYS = {
    "customerid": "customer_id", "customeruniqueid": "customer_id",
    "sellerid": "seller_id", "paymentreference": "payment_reference",
    "paymentreferences": "payment_reference", "shipmentid": "shipment_id",
    "shipmentids": "shipment_id", "itemid": "item_id", "itemids": "item_id",
}
TEXT_HINTS = ("message", "text", "complaint", "description", "body", "content", "statement",
              "subject", "summary", "details", "note", "claim", "reason", "type", "category")
DATE_KEYS = {"reportedat", "createdat", "submittedat", "receivedat", "openedat", "complaintdate",
             "reportdate", "ticketcreatedat", "casecreatedat", "contactedat"}
AMOUNT_KEYS = {"amount", "amountbrl", "claimedamount", "claimedamountbrl", "chargedamount",
               "chargedamountbrl", "expectedrefundbrl", "requestedrefundbrl"}
HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")

CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "late_delivery": (
        r"\blate\b", r"\bdelay", r"\batras", r"\bdemor", r"not (yet )?arrived",
        r"(hasn.?t|has not|never) arrived", r"\bnao chegou", r"\bainda nao (chegou|recebi)",
        r"\btre\b", r"\bcham\b", r"giao muon", r"chua (nhan|toi|den)", r"not received",
        r"never received", r"\bnao recebi", r"where is my order", r"\bprazo\b", r"\boverdue",
    ),
    "duplicate_charge": (
        r"\btwice\b", r"\bdouble", r"\bduplicat", r"two charges", r"charged 2",
        r"duas vezes", r"\bduplicad", r"hai lan", r"\b2 lan\b", r"cobrado 2",
    ),
    "payment_mismatch": (
        r"overcharg", r"wrong amount", r"incorrect amount", r"extra charge", r"more than",
        r"valor errado", r"cobrad[oa] a mais", r"valor diferente", r"sai so tien",
        r"tinh sai", r"thu thua", r"mismatch", r"charged more", r"different amount",
    ),
    "refund": (
        r"\brefund", r"\breembols", r"\bestorn", r"\bdevolu", r"hoan tien", r"money back",
        r"chargeback", r"\breimburs",
    ),
    "canceled": (r"\bcancel", r"\bhuy\b", r"\bda huy"),
    "unavailable": (
        r"unavailable", r"out of stock", r"indisponivel", r"sem estoque", r"het hang",
        r"not available",
    ),
}
CATEGORY_ISSUES: dict[str, frozenset[str]] = {
    "late_delivery": frozenset({"late_delivery_seller", "late_delivery_logistics"}),
    "duplicate_charge": frozenset({"duplicate_charge", "valid_split_payment", "payment_mismatch"}),
    "payment_mismatch": frozenset({"payment_mismatch", "duplicate_charge",
                                   "valid_split_payment"}),
    "refund": frozenset({"refund_pending", "refund_failed", "canceled_order_paid",
                         "unavailable_order_paid"}),
    "canceled": frozenset({"canceled_order_paid", "refund_pending", "refund_failed"}),
    "unavailable": frozenset({"unavailable_order_paid", "refund_pending", "refund_failed"}),
}
PAYMENT_CATEGORIES = frozenset({"duplicate_charge", "payment_mismatch", "refund", "canceled",
                                "unavailable"})


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def categorize(text: str) -> frozenset[str]:
    folded = fold(text).replace("_", " ").replace("-", " ")
    return frozenset(
        category
        for category, patterns in CATEGORY_PATTERNS.items()
        if any(re.search(pattern, folded) for pattern in patterns)
    )


def issues_for(categories: frozenset[str]) -> frozenset[str]:
    return frozenset().union(*(CATEGORY_ISSUES[c] for c in categories))


@dataclass(frozen=True)
class Claim:
    claim_id: str | None
    text: str
    categories: frozenset[str]
    amount: float | None


@dataclass(frozen=True)
class Intake:
    case_id: str
    order_ids: tuple[str, ...]
    lookup: dict[str, tuple[str, ...]]
    message: str
    claims: tuple[Claim, ...]
    categories: frozenset[str]
    reported_at: datetime | None


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := to_text(item))]
    text = to_text(value)
    return [text] if text else []


def _claim(raw: Any) -> Claim:
    if not isinstance(raw, dict):
        text = to_text(raw) or ""
        return Claim(None, text, categorize(text), None)
    claim_id = None
    texts: list[str] = []
    amount = None
    for key, value in raw.items():
        normalized = norm_key(key)
        if normalized in {"claimid", "id", "claimref"}:
            claim_id = to_text(value)
        elif normalized in AMOUNT_KEYS:
            amount = to_money(value)
        elif isinstance(value, str) and any(hint in normalized for hint in TEXT_HINTS):
            texts.append(value)
    text = " ".join(texts)
    return Claim(claim_id, text, categorize(text), amount)


def parse_intake(case: dict[str, Any]) -> Intake:
    order_ids: list[str] = []
    lookup: dict[str, list[str]] = {}
    texts: list[str] = []
    reported_at: datetime | None = None
    raw_claims: list[Any] = []
    for path, record in iter_dicts(case):
        if any("claim" in part for part in path):
            continue
        for key, value in record.items():
            normalized = norm_key(key)
            if normalized == "caseid":
                continue
            if normalized in ORDER_KEYS:
                order_ids.extend(_strings(value))
            elif normalized in LOOKUP_KEYS:
                lookup.setdefault(LOOKUP_KEYS[normalized], []).extend(_strings(value))
            elif normalized in DATE_KEYS and reported_at is None:
                reported_at = to_datetime(value)
            elif "claim" in normalized and isinstance(value, list):
                raw_claims.extend(value)
            elif isinstance(value, str) and any(hint in normalized for hint in TEXT_HINTS):
                texts.append(value)
    message = "\n".join(texts)
    if not order_ids:
        order_ids = HEX_ID.findall(message.lower())[:3]
    claims = tuple(_claim(raw) for raw in raw_claims[:5])
    categories = categorize(message).union(*(claim.categories for claim in claims))
    return Intake(
        case_id=case["case_id"],
        order_ids=tuple(dict.fromkeys(order_ids))[:5],
        lookup={name: tuple(dict.fromkeys(values))[:5] for name, values in lookup.items()},
        message=message,
        claims=claims,
        categories=frozenset(categories),
        reported_at=reported_at,
    )
