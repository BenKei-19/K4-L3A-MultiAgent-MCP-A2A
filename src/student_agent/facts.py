"""Normalize MCP evidence payloads into typed facts.

Evidence ``data`` is free-form, so every field is looked up through an alias table
that starts from the Olist column names. Calibrate the aliases here, not in the
agents, when the live gateway uses different names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

MONEY_TOLERANCE = 0.01

ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "orderid", "order_ref", "order_reference"),
    "order_status": ("order_status", "status", "state"),
    "customer_id": ("customer_id", "customer_unique_id"),
    "purchased_at": (
        "order_purchase_timestamp", "purchase_timestamp", "purchased_at", "created_at",
    ),
    "approved_at": ("order_approved_at", "approved_at"),
    "carrier_at": (
        "order_delivered_carrier_date", "delivered_carrier_date", "delivered_to_carrier_at",
        "handed_to_carrier_at", "carrier_handoff_at", "handoff_at", "picked_up_at", "shipped_at",
        "dispatched_at",
    ),
    "delivered_at": (
        "order_delivered_customer_date", "delivered_customer_date", "delivered_to_customer_at",
        "delivered_at", "delivery_date", "actual_delivery_date",
    ),
    "estimated_at": (
        "order_estimated_delivery_date", "estimated_delivery_date", "estimated_delivery_at",
        "estimated_delivery", "promised_delivery_date", "expected_delivery_date", "eta",
    ),
    "shipping_limit_at": (
        "shipping_limit_date", "shipping_limit", "seller_shipping_limit", "ship_by",
        "handoff_deadline", "seller_deadline",
    ),
    "order_total": (
        "order_total", "order_total_brl", "total_amount", "total_amount_brl", "total_brl",
        "order_value", "amount_due", "grand_total",
    ),
    "item_id": ("item_id", "order_item_ref", "line_id"),
    "order_item_id": ("order_item_id", "item_sequence", "line_number"),
    "product_id": ("product_id",),
    "seller_id": ("seller_id", "merchant_id", "vendor_id"),
    "price": ("price", "price_brl", "item_price", "unit_price"),
    "freight": ("freight_value", "freight", "freight_brl", "shipping_fee", "shipping_cost"),
    "payment_reference": (
        "payment_reference", "payment_ref", "payment_id", "transaction_id", "reference",
    ),
    "payment_sequential": ("payment_sequential", "sequence", "sequential"),
    "payment_type": ("payment_type", "payment_method", "method", "type"),
    "installments": ("payment_installments", "installments"),
    "payment_value": (
        "payment_value", "amount_brl", "amount", "value", "captured_amount", "paid_amount",
    ),
    "payment_status": ("payment_status", "capture_status", "status", "state"),
    "paid_at": ("captured_at", "paid_at", "created_at", "timestamp"),
    "payment_refunded": ("refunded_amount", "refunded_amount_brl", "amount_refunded"),
    "refund_id": ("refund_id", "refund_reference", "refund_ref", "id"),
    "refund_amount": ("refund_amount", "refund_amount_brl", "amount_brl", "amount", "value"),
    "refund_status": ("refund_status", "status", "state"),
    "shipment_id": ("shipment_id", "tracking_id", "tracking_number", "tracking_code"),
    "carrier_id": ("carrier_id", "logistics_provider_id", "carrier", "carrier_name"),
    "shipment_status": ("shipment_status", "status", "state"),
}

CANCELED_STATUSES = {"canceled", "cancelled"}
UNAVAILABLE_STATUSES = {"unavailable"}
PAYMENT_FAILED = {
    "failed", "declined", "voided", "void", "canceled", "cancelled", "rejected", "error",
    "not_captured", "authorization_failed",
}
REFUND_COMPLETED = {"completed", "complete", "succeeded", "success", "processed", "refunded",
                    "done", "settled", "paid"}
REFUND_PENDING = {"pending", "processing", "requested", "initiated", "in_progress", "queued",
                  "created", "open", "submitted"}
REFUND_FAILED = {"failed", "rejected", "declined", "error", "reversed", "returned", "bounced"}


def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_NORMALIZED = {
    name: tuple(norm_key(alias) for alias in aliases) for name, aliases in ALIASES.items()
}


def pick(record: dict[str, Any], name: str) -> Any:
    lowered = {norm_key(key): value for key, value in record.items()}
    for alias in _NORMALIZED[name]:
        value = lowered.get(alias)
        if value is not None and value != "":
            return value
    return None


def has_any(record: dict[str, Any], *names: str) -> bool:
    return any(pick(record, name) is not None for name in names)


def iter_dicts(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], dict]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from iter_dicts(child, (*path, str(key).lower()))
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child, path)


def to_text(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    text = str(value).strip()
    return text or None


def to_money(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return round(float(value), 2)
    if isinstance(value, str):
        cleaned = value.replace("R$", "").replace(" ", "").strip()
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return round(float(cleaned), 2)
        except ValueError:
            return None
    return None


def to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def money_equal(left: float, right: float) -> bool:
    return abs(left - right) <= MONEY_TOLERANCE


def status_of(value: Any) -> str | None:
    text = to_text(value)
    return text.lower().replace(" ", "_").replace("-", "_") if text else None


@dataclass
class OrderFact:
    ref: str
    order_id: str | None
    status: str | None
    customer_id: str | None
    purchased_at: datetime | None
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    declared_total: float | None


@dataclass
class ItemFact:
    ref: str
    item_id: str | None
    product_id: str | None
    seller_id: str | None
    shipping_limit_at: datetime | None
    price: float
    freight: float


@dataclass
class PaymentFact:
    ref: str
    reference: str | None
    sequential: int | None
    payment_type: str | None
    installments: int | None
    value: float
    status: str | None
    paid_at: datetime | None

    @property
    def captured(self) -> bool:
        return self.status not in PAYMENT_FAILED

    @property
    def signature(self) -> tuple[str | None, float, int | None]:
        return (self.payment_type, self.value, self.installments)


@dataclass
class RefundFact:
    ref: str
    refund_id: str | None
    amount: float
    status: str | None
    payment_reference: str | None

    @property
    def state(self) -> str:
        if self.status in REFUND_FAILED:
            return "failed"
        if self.status in REFUND_PENDING:
            return "pending"
        if self.status in REFUND_COMPLETED or self.status is None:
            return "completed"
        return "pending"


@dataclass
class ShipmentFact:
    ref: str
    shipment_id: str | None
    seller_id: str | None
    carrier_id: str | None
    status: str | None
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    shipping_limit_at: datetime | None


@dataclass
class CaseFacts:
    orders: list[OrderFact] = field(default_factory=list)
    items: list[ItemFact] = field(default_factory=list)
    payments: list[PaymentFact] = field(default_factory=list)
    refunds: list[RefundFact] = field(default_factory=list)
    shipments: list[ShipmentFact] = field(default_factory=list)
    seller_refs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def order(self) -> OrderFact | None:
        return self.orders[0] if self.orders else None

    def captured_payments(self) -> list[PaymentFact]:
        return [payment for payment in self.payments if payment.captured]

    def captured_total(self) -> float:
        return round(sum(payment.value for payment in self.captured_payments()), 2)

    def items_total(self) -> float | None:
        if not self.items:
            return None
        return round(sum(item.price + item.freight for item in self.items), 2)

    def refunded_total(self, state: str = "completed") -> float:
        return round(sum(r.amount for r in self.refunds if r.state == state), 2)

    def seller_ids(self) -> list[str]:
        ids = {item.seller_id for item in self.items if item.seller_id}
        ids |= {shipment.seller_id for shipment in self.shipments if shipment.seller_id}
        return sorted(ids)


def _path_mentions(path: tuple[str, ...], *words: str) -> bool:
    return any(word in part for part in path for word in words)


def extract_order(ref: str, data: Any) -> list[OrderFact]:
    facts: list[OrderFact] = []
    for path, record in iter_dicts(data):
        if _path_mentions(path, "item", "payment", "refund", "shipment", "seller"):
            continue
        order_id = to_text(pick(record, "order_id"))
        if order_id is None and not has_any(record, "purchased_at", "estimated_at"):
            continue
        status = pick(record, "order_status")
        if order_id is None and status is None:
            continue
        facts.append(
            OrderFact(
                ref=ref,
                order_id=order_id,
                status=status_of(status),
                customer_id=to_text(pick(record, "customer_id")),
                purchased_at=to_datetime(pick(record, "purchased_at")),
                carrier_at=to_datetime(pick(record, "carrier_at")),
                delivered_at=to_datetime(pick(record, "delivered_at")),
                estimated_at=to_datetime(pick(record, "estimated_at")),
                declared_total=to_money(pick(record, "order_total")),
            )
        )
        break
    return facts


def extract_items(ref: str, data: Any) -> list[ItemFact]:
    facts: list[ItemFact] = []
    for path, record in iter_dicts(data):
        if _path_mentions(path, "payment", "refund", "shipment"):
            continue
        price = to_money(pick(record, "price"))
        if price is None:
            continue
        facts.append(
            ItemFact(
                ref=ref,
                item_id=to_text(pick(record, "item_id")) or to_text(pick(record, "order_item_id")),
                product_id=to_text(pick(record, "product_id")),
                seller_id=to_text(pick(record, "seller_id")),
                shipping_limit_at=to_datetime(pick(record, "shipping_limit_at")),
                price=price,
                freight=to_money(pick(record, "freight")) or 0.0,
            )
        )
    return facts


REFUND_KEYS = {"refundid", "refundstatus", "refundamount", "refundamountbrl", "refundreference"}


def _is_refund_record(path: tuple[str, ...], record: dict[str, Any]) -> bool:
    keys = {norm_key(key) for key in record}
    return _path_mentions(path, "refund") or bool(keys & REFUND_KEYS)


def extract_payments(ref: str, data: Any) -> tuple[list[PaymentFact], list[RefundFact]]:
    payments: list[PaymentFact] = []
    refunds: list[RefundFact] = []
    for path, record in iter_dicts(data):
        if _is_refund_record(path, record):
            amount = to_money(pick(record, "refund_amount"))
            if amount is None:
                continue
            refunds.append(
                RefundFact(
                    ref=ref,
                    refund_id=to_text(pick(record, "refund_id")),
                    amount=amount,
                    status=status_of(pick(record, "refund_status")),
                    payment_reference=to_text(pick(record, "payment_reference")),
                )
            )
            continue
        value = to_money(pick(record, "payment_value"))
        if value is None or not has_any(
            record, "payment_type", "payment_sequential", "payment_reference", "installments"
        ):
            continue
        sequential = pick(record, "payment_sequential")
        installments = pick(record, "installments")
        payments.append(
            PaymentFact(
                ref=ref,
                reference=to_text(pick(record, "payment_reference")),
                sequential=int(sequential) if str(sequential).isdigit() else None,
                payment_type=status_of(pick(record, "payment_type")),
                installments=int(installments) if str(installments).isdigit() else None,
                value=value,
                status=status_of(pick(record, "payment_status")),
                paid_at=to_datetime(pick(record, "paid_at")),
            )
        )
        refunded = to_money(pick(record, "payment_refunded"))
        if refunded:
            refunds.append(
                RefundFact(
                    ref=ref,
                    refund_id=None,
                    amount=refunded,
                    status=status_of(pick(record, "refund_status")),
                    payment_reference=payments[-1].reference,
                )
            )
    if not refunds:
        # A payment marked "refunded" with no refund record still returned the money.
        refunds = [
            RefundFact(p.ref, None, p.value, "refunded", p.reference)
            for p in payments
            if p.status == "refunded"
        ]
    return payments, refunds


def extract_shipments(ref: str, data: Any) -> list[ShipmentFact]:
    facts: list[ShipmentFact] = []
    for path, record in iter_dicts(data):
        if _path_mentions(path, "payment", "refund"):
            continue
        if not has_any(record, "shipment_id", "carrier_at", "delivered_at", "estimated_at"):
            continue
        facts.append(
            ShipmentFact(
                ref=ref,
                shipment_id=to_text(pick(record, "shipment_id")),
                seller_id=to_text(pick(record, "seller_id")),
                carrier_id=to_text(pick(record, "carrier_id")),
                status=status_of(pick(record, "shipment_status")),
                carrier_at=to_datetime(pick(record, "carrier_at")),
                delivered_at=to_datetime(pick(record, "delivered_at")),
                estimated_at=to_datetime(pick(record, "estimated_at")),
                shipping_limit_at=to_datetime(pick(record, "shipping_limit_at")),
            )
        )
    return facts


def extract_ids(data: Any) -> dict[str, set[str]]:
    """Collect entity identifiers present in an evidence payload (for follow-up calls)."""
    found: dict[str, set[str]] = {}
    for _, record in iter_dicts(data):
        for name in ("order_id", "seller_id", "shipment_id", "payment_reference", "item_id",
                     "product_id"):
            value = to_text(pick(record, name))
            if value:
                found.setdefault(name, set()).add(value)
    return found
