"""Deterministic business rules: CaseFacts -> ranked findings -> one primary finding.

Detection is evidence-first. The customer's claim categories only break ties between
issues that the evidence already shows, and pick the family for no-issue fallbacks.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .facts import (
    CANCELED_STATUSES,
    MONEY_TOLERANCE,
    UNAVAILABLE_STATUSES,
    CaseFacts,
    PaymentFact,
    money_equal,
)
from .intake import PAYMENT_CATEGORIES, Intake, issues_for

OPEN_STATUSES = {"shipped", "invoiced", "processing", "approved", "created", "in_transit"}
DUPLICATE_WINDOW = timedelta(minutes=30)

ISSUE_DOMAINS: dict[str, set[str]] = {
    "canceled_order_paid": {"order", "payment", "refund"},
    "unavailable_order_paid": {"order", "payment", "refund", "item"},
    "late_delivery_seller": {"order", "item", "shipment", "seller"},
    "late_delivery_logistics": {"order", "item", "shipment"},
    "valid_split_payment": {"order", "payment", "item"},
    "payment_mismatch": {"order", "payment", "item"},
    "duplicate_charge": {"order", "payment", "item"},
    "refund_pending": {"order", "payment", "refund"},
    "refund_failed": {"order", "payment", "refund"},
}
CITABLE_DOMAINS = {"order", "item", "payment", "refund", "shipment", "seller", "policy"}


@dataclass
class RefundLine:
    reason_code: str
    amount: float
    entity_id: str | None


@dataclass
class Finding:
    issue: str
    status: str
    confidence: float
    causes: list[str]
    parties: list[tuple[str, str | None]]
    refund_lines: list[RefundLine]
    actions: list[str]
    domains: set[str]
    bases: dict[str, float] = field(default_factory=dict)

    @property
    def refund_total(self) -> float:
        return round(sum(line.amount for line in self.refund_lines), 2)


@dataclass
class Timeline:
    estimated_at: datetime | None = None
    delivered_at: datetime | None = None
    carrier_at: datetime | None = None
    limits: dict[str, datetime] = field(default_factory=dict)
    late: bool | None = None
    late_sellers: list[str] = field(default_factory=list)
    carrier_id: str | None = None


@dataclass
class Analysis:
    facts: CaseFacts
    expected_total: float | None
    total_source: str | None
    captured_total: float
    outstanding: float
    timeline: Timeline
    conflicts: list[dict]
    detected: list[Finding]


def _conflict(field_name: str, sources: list[str], selected: str, code: str) -> dict:
    return {"field": field_name, "sources": sources, "selected_source": selected,
            "resolution_code": code}


def _merge_date(
    name: str, order_value: datetime | None, shipment_value: datetime | None, conflicts: list
) -> datetime | None:
    if order_value and shipment_value and order_value.date() != shipment_value.date():
        conflicts.append(_conflict(name, ["order", "shipment"], "shipment",
                                   "PREFER_SHIPMENT_TRACKING"))
    return shipment_value or order_value


def _first_shipment_value(facts: CaseFacts, attr: str) -> datetime | None:
    return next((getattr(s, attr) for s in facts.shipments if getattr(s, attr)), None)


def build_timeline(facts: CaseFacts, reported_at: datetime | None, conflicts: list) -> Timeline:
    order = facts.order
    timeline = Timeline(carrier_id=next((s.carrier_id for s in facts.shipments if s.carrier_id),
                                        None))
    for name, attr in (("estimated_delivery_date", "estimated_at"),
                       ("delivered_customer_date", "delivered_at"),
                       ("delivered_carrier_date", "carrier_at")):
        merged = _merge_date(name, getattr(order, attr) if order else None,
                             _first_shipment_value(facts, attr), conflicts)
        setattr(timeline, attr, merged)
    for item in facts.items:
        if item.seller_id and item.shipping_limit_at:
            current = timeline.limits.get(item.seller_id)
            if current is None or item.shipping_limit_at < current:
                timeline.limits[item.seller_id] = item.shipping_limit_at
    for s in facts.shipments:
        if s.seller_id and s.shipping_limit_at and s.seller_id not in timeline.limits:
            timeline.limits[s.seller_id] = s.shipping_limit_at

    status = order.status if order else None
    if status in CANCELED_STATUSES | UNAVAILABLE_STATUSES or timeline.estimated_at is None:
        return timeline
    if timeline.delivered_at:
        timeline.late = timeline.delivered_at.date() > timeline.estimated_at.date()
    elif reported_at and (status in OPEN_STATUSES or status is None):
        timeline.late = reported_at.date() > timeline.estimated_at.date()
    if not timeline.late:
        return timeline

    handoffs = {s.seller_id: s.carrier_at for s in facts.shipments if s.seller_id and s.carrier_at}
    for seller_id, limit in sorted(timeline.limits.items()):
        handoff = handoffs.get(seller_id, timeline.carrier_at)
        if handoff is not None:
            if handoff > limit:
                timeline.late_sellers.append(seller_id)
        elif reported_at and reported_at > limit:
            timeline.late_sellers.append(seller_id)
    return timeline


def find_duplicates(
    captured: list[PaymentFact], captured_total: float, expected: float | None
) -> list[PaymentFact]:
    explicit = [p for p in captured if p.status and "duplicat" in p.status]
    if explicit:
        return explicit
    groups: dict[tuple[str | None, float], list[PaymentFact]] = defaultdict(list)
    for payment in captured:
        groups[(payment.payment_type, payment.value)].append(payment)
    for (_, value), group in groups.items():
        if len(group) < 2 or value <= 0:
            continue
        ordered = sorted(group, key=lambda p: (p.sequential or 0, p.paid_at or datetime.min))
        if expected is not None:
            excess = round(captured_total - expected, 2)
            copies = round(excess / value)
            if excess > MONEY_TOLERANCE and 1 <= copies < len(group) and money_equal(
                excess, copies * value
            ):
                return ordered[-copies:]
        elif all(p.paid_at for p in ordered) and (
            ordered[-1].paid_at - ordered[0].paid_at <= DUPLICATE_WINDOW
        ):
            return ordered[1:]
    return []


def analyze(facts: CaseFacts, intake: Intake) -> Analysis:
    conflicts: list[dict] = []
    order = facts.order
    order_id = order.order_id if order else None
    items_total = facts.items_total()
    declared = order.declared_total if order else None
    if items_total is not None and declared is not None and not money_equal(items_total, declared):
        conflicts.append(_conflict("order_total", ["order", "item"], "item",
                                   "RECOMPUTED_FROM_ITEM_LINES"))
    expected = items_total if items_total is not None else declared
    total_source = "item" if items_total is not None else ("order" if declared else None)
    captured = facts.captured_payments()
    captured_total = facts.captured_total()
    completed = facts.refunded_total("completed")
    outstanding = round(max(0.0, captured_total - completed), 2)
    timeline = build_timeline(facts, intake.reported_at, conflicts)
    status = order.status if order else None
    detected: list[Finding] = []

    failed = [r for r in facts.refunds if r.state == "failed"]
    retried = {round(r.amount, 2) for r in facts.refunds if r.state != "failed"}
    failed = [r for r in failed if round(r.amount, 2) not in retried]
    if failed:
        amount = round(sum(r.amount for r in failed), 2)
        amount = min(amount, outstanding) if outstanding > MONEY_TOLERANCE else amount
        entity = failed[0].refund_id or failed[0].payment_reference or order_id
        detected.append(Finding(
            "refund_failed", "action_required", 0.85, ["REFUND_FAILED", "REFUND_PROCESSING_FAILED"],
            [("payment_provider", None)], [RefundLine("FAILED_REFUND_REISSUE", amount, entity)],
            ["retry_refund", "notify_customer"], ISSUE_DOMAINS["refund_failed"],
            {"failed": amount, "outstanding": outstanding},
        ))
    pending = [r for r in facts.refunds if r.state == "pending"]
    if pending:
        amount = round(sum(r.amount for r in pending), 2)
        entity = pending[0].refund_id or pending[0].payment_reference or order_id
        detected.append(Finding(
            "refund_pending", "action_required", 0.82, ["REFUND_PENDING", "REFUND_NOT_SETTLED"],
            [("payment_provider", None)], [RefundLine("PENDING_REFUND_COMPLETION", amount, entity)],
            ["escalate_pending_refund", "notify_customer"], ISSUE_DOMAINS["refund_pending"],
            {"pending": amount, "outstanding": outstanding},
        ))

    closed = status in CANCELED_STATUSES | UNAVAILABLE_STATUSES
    if closed and outstanding > MONEY_TOLERANCE:
        if status in CANCELED_STATUSES:
            detected.append(Finding(
                "canceled_order_paid", "action_required", 0.88,
                ["CANCELED_ORDER_PAID", "CANCELED_ORDER_NOT_REFUNDED"], [("platform", None)],
                [RefundLine("CANCELED_ORDER_REFUND", outstanding, order_id)],
                ["refund_customer", "notify_customer"], ISSUE_DOMAINS["canceled_order_paid"],
                {"outstanding": outstanding},
            ))
        else:
            sellers = facts.seller_ids()
            detected.append(Finding(
                "unavailable_order_paid", "action_required", 0.85,
                ["UNAVAILABLE_ORDER_PAID", "UNAVAILABLE_ORDER_NOT_REFUNDED"],
                [("seller", s) for s in sellers] or [("seller", None)],
                [RefundLine("UNAVAILABLE_ORDER_REFUND", outstanding, order_id)],
                ["refund_customer", "notify_seller", "notify_customer"],
                ISSUE_DOMAINS["unavailable_order_paid"], {"outstanding": outstanding},
            ))

    duplicates = [] if closed else find_duplicates(captured, captured_total, expected)
    if duplicates:
        amount = round(sum(p.value for p in duplicates), 2)
        detected.append(Finding(
            "duplicate_charge", "action_required", 0.85,
            ["DUPLICATE_CHARGE", "DUPLICATE_PAYMENT_CAPTURE"], [("payment_provider", None)],
            [RefundLine("DUPLICATE_CHARGE_REFUND", p.value, p.reference or order_id)
             for p in duplicates],
            ["refund_duplicate_charge", "notify_customer"], ISSUE_DOMAINS["duplicate_charge"],
            {"duplicate": amount},
        ))
    elif not closed and expected is not None and captured and not money_equal(
        captured_total, expected
    ):
        difference = round(captured_total - expected, 2)
        if difference > 0:
            detected.append(Finding(
                "payment_mismatch", "action_required", 0.78,
                ["PAYMENT_MISMATCH", "CAPTURED_TOTAL_EXCEEDS_ORDER_TOTAL"],
                [("payment_provider", None)],
                [RefundLine("OVERCHARGE_REFUND", difference, order_id)],
                ["refund_excess_charge", "reconcile_payment"], ISSUE_DOMAINS["payment_mismatch"],
                {"excess": difference},
            ))
        else:
            detected.append(Finding(
                "payment_mismatch", "needs_investigation", 0.7,
                ["PAYMENT_MISMATCH", "CAPTURED_TOTAL_BELOW_ORDER_TOTAL"],
                [("payment_provider", None)], [], ["reconcile_payment"],
                ISSUE_DOMAINS["payment_mismatch"], {"shortfall": -difference},
            ))

    if timeline.late:
        freight = round(sum(i.freight for i in facts.items), 2)
        if timeline.late_sellers:
            late_freight = round(
                sum(i.freight for i in facts.items if i.seller_id in timeline.late_sellers), 2
            )
            detected.append(Finding(
                "late_delivery_seller", "action_required", 0.82,
                ["LATE_DELIVERY_SELLER", "SELLER_MISSED_SHIPPING_LIMIT"],
                [("seller", s) for s in timeline.late_sellers],
                [RefundLine("LATE_DELIVERY_FREIGHT_REFUND", late_freight, order_id)]
                if late_freight > 0 else [],
                ["compensate_customer", "notify_seller"], ISSUE_DOMAINS["late_delivery_seller"],
                {"freight": late_freight, "items_total": expected or 0.0},
            ))
        else:
            detected.append(Finding(
                "late_delivery_logistics", "action_required",
                0.8 if timeline.limits else 0.62,
                ["LATE_DELIVERY_LOGISTICS", "CARRIER_TRANSIT_DELAY"],
                [("logistics_provider", timeline.carrier_id)],
                [RefundLine("LATE_DELIVERY_FREIGHT_REFUND", freight, order_id)]
                if freight > 0 else [],
                ["compensate_customer", "open_carrier_claim"],
                ISSUE_DOMAINS["late_delivery_logistics"],
                {"freight": freight, "items_total": expected or 0.0},
            ))

    return Analysis(facts, expected, total_source, captured_total, outstanding, timeline,
                    conflicts, detected)


def _no_issue(analysis: Analysis, intake: Intake, available: set[str]) -> Finding:
    facts = analysis.facts
    categories = intake.categories
    payment_claim = bool(categories & PAYMENT_CATEGORIES)
    delivery_claim = "late_delivery" in categories
    captured = facts.captured_payments()
    if (
        len(captured) >= 2
        and analysis.expected_total is not None
        and money_equal(analysis.captured_total, analysis.expected_total)
        and (payment_claim or not categories)
    ):
        return Finding(
            "valid_split_payment", "no_action", 0.84, ["VALID_SPLIT_PAYMENT",
                                                       "SPLIT_PAYMENT_RECONCILED"],
            [], [], [], ISSUE_DOMAINS["valid_split_payment"],
        )
    missing_payment = payment_claim and "payment" not in available
    missing_timeline = delivery_claim and analysis.timeline.estimated_at is None
    if missing_payment or missing_timeline:
        return insufficient(available)
    domains = {"order"}
    if delivery_claim:
        domains |= {"shipment", "item"}
    if payment_claim:
        domains |= {"payment", "refund", "item"}
    if not categories:
        domains |= {"payment", "shipment"}
    return Finding(
        "unsupported_claim", "no_action", 0.74,
        ["UNSUPPORTED_CLAIM", "CLAIM_CONTRADICTED_BY_EVIDENCE"], [], [], [], domains,
    )


def insufficient(available: set[str]) -> Finding:
    return Finding(
        "insufficient_evidence", "needs_investigation", 0.7,
        ["INSUFFICIENT_EVIDENCE", "AUTHORITATIVE_EVIDENCE_MISSING"], [("unknown", None)], [],
        ["request_additional_information", "escalate_manual_review"],
        available & {"order", "item", "payment", "refund", "shipment"},
    )


def decide(analysis: Analysis, intake: Intake, available: set[str]) -> Finding:
    if analysis.facts.order is None:
        return insufficient(available)
    claimed = issues_for(intake.categories)
    chosen: Finding | None = None
    if analysis.detected:
        matching = [f for f in analysis.detected if f.issue in claimed]
        chosen = matching[0] if matching else analysis.detected[0]
        if claimed:
            chosen.confidence += 0.04 if matching else -0.06
    else:
        chosen = _no_issue(analysis, intake, available)
    if analysis.conflicts:
        chosen.confidence -= 0.06
    chosen.confidence = round(min(0.95, max(0.35, chosen.confidence)), 2)
    return chosen
