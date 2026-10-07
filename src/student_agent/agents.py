"""Specialist agents. Each one may only call tools in its own domains."""

from __future__ import annotations

import asyncio
from typing import Any

from .a2a import ORDER_AGENT, PAYMENT_AGENT, POLICY_AGENT, SHIPMENT_AGENT, A2ABus, A2AMessage
from .decision import Finding
from .evidence import Evidence, EvidenceStore, ToolRouter
from .facts import (
    CaseFacts,
    extract_ids,
    extract_items,
    extract_order,
    extract_payments,
    extract_shipments,
)
from .policy import PolicyRules, apply_policy, interpret_policy

ENTITY_KEYS = ("seller_id", "shipment_id", "payment_reference", "item_id", "product_id")


class Specialist:
    name = "specialist"
    domains: frozenset[str] = frozenset()

    def __init__(self, router: ToolRouter, store: EvidenceStore, bus: A2ABus) -> None:
        self.router = router
        self.store = store
        self.bus = bus

    def has_tools(self, domains: set[str] | frozenset[str] | None = None) -> bool:
        return bool(self.router.tools_for(set(domains or self.domains)))

    async def collect(
        self,
        domains: set[str] | frozenset[str],
        context: dict[str, list[str]],
        hints: list[str] | None = None,
        rounds: int = 2,
    ) -> list[Evidence]:
        """Call every feasible tool; later rounds only fill domains still missing."""
        allowed = set(domains) & self.domains
        context = {key: list(values) for key, values in context.items()}
        collected: list[Evidence] = []
        covered: set[str] = set()
        for round_number in range(rounds):
            planned = []
            for spec in self.router.tools_for(allowed):
                if round_number and self.router.domains[spec.name] in covered:
                    continue
                for args in self.router.plan_calls(spec, context, hints or []):
                    if not self.store.called(spec.name, args):
                        planned.append((spec, args))
            if not planned:
                break
            results = await asyncio.gather(
                *(self.store.fetch(self.name, spec, args) for spec, args in planned)
            )
            for (spec, _), evidence in zip(planned, results, strict=True):
                if evidence is None:
                    continue
                collected.append(evidence)
                covered.add(self.router.domains[spec.name])
                for key, values in extract_ids(evidence.data).items():
                    if key in ENTITY_KEYS:
                        known = context.setdefault(key, [])
                        known.extend(v for v in sorted(values) if v not in known)
        return collected

    def failures(self) -> int:
        return sum(1 for failure in self.store.failures if failure.actor == self.name)


class OrderAgent(Specialist):
    name = ORDER_AGENT
    domains = frozenset({"order", "item", "seller"})

    async def collect_order(self, task: A2AMessage) -> A2AMessage:
        context = {"order_id": list(task.payload.get("order_ids", []))}
        context.update({k: list(v) for k, v in task.payload.get("lookup", {}).items()})
        evidence = await self.collect({"order", "item"}, context)
        orders = [fact for e in evidence if e.domain == "order"
                  for fact in extract_order(e.ref, e.data)]
        found = bool(orders)
        ids: dict[str, set[str]] = {}
        for item in evidence:
            for key, values in extract_ids(item.data).items():
                ids.setdefault(key, set()).update(values)
        verified = [o.order_id for o in orders if o.order_id] or (
            list(task.payload.get("order_ids", []))[:1] if found else []
        )
        return self.bus.reply(
            task,
            "ORDER_EVIDENCE_READY" if found else "ORDER_NOT_FOUND",
            {"order_ids": verified,
             **{key: sorted(values) for key, values in ids.items() if key in ENTITY_KEYS},
             "failures": self.failures()},
            [e.ref for e in evidence],
            status="ok" if found else "not_found",
        )

    async def collect_sellers(self, task: A2AMessage) -> A2AMessage:
        evidence = await self.collect(
            {"seller"}, {"seller_id": list(task.payload.get("seller_ids", []))}, rounds=1
        )
        return self.bus.reply(
            task, "SELLER_EVIDENCE_READY" if evidence else "SELLER_EVIDENCE_MISSING",
            {"sellers": len(evidence)}, [e.ref for e in evidence],
            status="ok" if evidence else "missing",
        )


class PaymentAgent(Specialist):
    name = PAYMENT_AGENT
    domains = frozenset({"payment", "refund"})

    async def run(self, task: A2AMessage) -> A2AMessage:
        evidence = await self.collect(self.domains, dict(task.payload.get("context", {})))
        payments, refunds = [], []
        for item in evidence:
            found_payments, found_refunds = extract_payments(item.ref, item.data)
            payments += found_payments
            refunds += found_refunds
        return self.bus.reply(
            task,
            "PAYMENT_EVIDENCE_READY" if evidence else "PAYMENT_EVIDENCE_MISSING",
            {"payments": len(payments), "refunds": len(refunds),
             "captured_brl": round(sum(p.value for p in payments if p.captured), 2),
             "failures": self.failures()},
            [e.ref for e in evidence],
            status="ok" if evidence else "missing",
        )


class ShipmentAgent(Specialist):
    name = SHIPMENT_AGENT
    domains = frozenset({"shipment"})

    async def run(self, task: A2AMessage) -> A2AMessage:
        evidence = await self.collect(self.domains, dict(task.payload.get("context", {})))
        shipments = [s for e in evidence for s in extract_shipments(e.ref, e.data)]
        return self.bus.reply(
            task,
            "SHIPMENT_EVIDENCE_READY" if evidence else "SHIPMENT_EVIDENCE_MISSING",
            {"shipments": len(shipments), "failures": self.failures()},
            [e.ref for e in evidence],
            status="ok" if evidence else "missing",
        )


class PolicyAgent(Specialist):
    name = POLICY_AGENT
    domains = frozenset({"policy"})

    async def run(
        self, task: A2AMessage, finding: Finding, recipient: str
    ) -> tuple[A2AMessage, PolicyRules]:
        issue = finding.issue
        hints = [issue, issue.replace("_", " "), *task.payload.get("categories", [])]
        evidence = await self.collect(
            self.domains, dict(task.payload.get("context", {})), hints, rounds=1
        )
        rules = interpret_policy(evidence, issue)
        apply_policy(finding, rules)
        self.bus.trace.emit(
            case_id=self.bus.case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=issue,
            evidence_refs=(rules.matched or rules.consulted)[:20] or None,
            attributes={
                "case_status": finding.status,
                "refund_brl": finding.refund_total,
                "policy_source": rules.source,
                "actions": len(finding.actions),
            },
        )
        reply = self.bus.reply(
            task, "POLICY_APPLIED",
            {"primary_issue": issue, "case_status": finding.status,
             "refund_brl": finding.refund_total, "policy_source": rules.source},
            rules.matched or rules.consulted,
            recipient=recipient,
        )
        return reply, rules


def build_facts(store: EvidenceStore) -> CaseFacts:
    """Merge specialist evidence into one fact sheet, preferring dedicated domains."""
    facts = CaseFacts()
    orders = store.by_domain("order")
    for evidence in orders:
        facts.orders += extract_order(evidence.ref, evidence.data)
        facts.warnings += list(evidence.warnings)

    item_sources = store.by_domain("item") or orders
    seen_items: set[Any] = set()
    for evidence in item_sources:
        for item in extract_items(evidence.ref, evidence.data):
            key = (item.item_id, item.product_id, item.seller_id, item.price, item.freight)
            if item.item_id is None or key not in seen_items:
                seen_items.add(key)
                facts.items.append(item)

    payment_sources = store.by_domain("payment", "refund") or orders
    seen_payments: set[Any] = set()
    seen_refunds: set[Any] = set()
    for evidence in payment_sources:
        payments, refunds = extract_payments(evidence.ref, evidence.data)
        for payment in payments:
            key = payment.reference or (payment.sequential, payment.payment_type, payment.value)
            if key not in seen_payments:
                seen_payments.add(key)
                facts.payments.append(payment)
        for refund in refunds:
            key = refund.refund_id or (refund.amount, refund.status, refund.payment_reference)
            if key not in seen_refunds:
                seen_refunds.add(key)
                facts.refunds.append(refund)

    seen_shipments: set[Any] = set()
    for evidence in store.by_domain("shipment"):
        for shipment in extract_shipments(evidence.ref, evidence.data):
            key = shipment.shipment_id or (shipment.seller_id, shipment.carrier_at)
            if key not in seen_shipments:
                seen_shipments.add(key)
                facts.shipments.append(shipment)
        facts.warnings += list(evidence.warnings)
    for evidence in store.by_domain("seller"):
        for seller_id in extract_ids(evidence.data).get("seller_id", ()):
            facts.seller_refs[seller_id] = evidence.ref
    return facts
