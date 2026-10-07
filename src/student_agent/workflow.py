from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from .a2a import (
    COORDINATOR,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER,
    A2ABus,
    A2AMessage,
)
from .agents import OrderAgent, PaymentAgent, PolicyAgent, ShipmentAgent, build_facts
from .decision import analyze, decide
from .evidence import EvidenceStore, Gateway, ToolRouter
from .intake import Intake, parse_intake
from .policy import PolicyRules
from .trace import TraceWriter
from .verifier import build_output, verify

SPECIALIST_TIMEOUT_SECONDS = 240.0


class Coordinator:
    """Owns the case: delegates evidence collection, decides, then hands off to verify."""

    def __init__(self, intake: Intake, router: ToolRouter, store: EvidenceStore, bus: A2ABus,
                 trace: TraceWriter) -> None:
        self.intake = intake
        self.store = store
        self.bus = bus
        self.trace = trace
        self.order_agent = OrderAgent(router, store, bus)
        self.payment_agent = PaymentAgent(router, store, bus)
        self.shipment_agent = ShipmentAgent(router, store, bus)
        self.policy_agent = PolicyAgent(router, store, bus)

    async def _await(self, task: A2AMessage, work: Awaitable[Any]) -> Any:
        try:
            return await asyncio.wait_for(work, SPECIALIST_TIMEOUT_SECONDS)
        except TimeoutError:
            return self.bus.reply(task, "SPECIALIST_TIMEOUT", {}, status="timeout")

    async def run(self) -> dict[str, Any]:
        intake, bus = self.intake, self.bus
        order_task = bus.assign(COORDINATOR, ORDER_AGENT, "COLLECT_ORDER_EVIDENCE",
                                {"order_ids": list(intake.order_ids), "lookup": intake.lookup})
        order_reply = await self._await(order_task, self.order_agent.collect_order(order_task))

        if order_reply.status == "ok":
            context = {key: list(order_reply.payload.get(key, []))
                       for key in ("order_id", "seller_id", "shipment_id", "payment_reference",
                                   "item_id")}
            context["order_id"] = list(order_reply.payload.get("order_ids", []))
            payment_task = bus.assign(COORDINATOR, PAYMENT_AGENT, "COLLECT_PAYMENT_EVIDENCE",
                                      {"context": context})
            shipment_task = bus.assign(COORDINATOR, SHIPMENT_AGENT, "COLLECT_SHIPMENT_EVIDENCE",
                                       {"context": context})
            await asyncio.gather(
                self._await(payment_task, self.payment_agent.run(payment_task)),
                self._await(shipment_task, self.shipment_agent.run(shipment_task)),
            )
        else:
            context = {"order_id": []}

        facts = build_facts(self.store)
        available = {evidence.domain for evidence in self.store.items}
        analysis = analyze(facts, intake)
        finding = decide(analysis, intake, available)

        if finding.issue == "late_delivery_seller" and self.order_agent.has_tools({"seller"}):
            late_sellers = [party_id for _, party_id in finding.parties if party_id]
            seller_task = bus.assign(COORDINATOR, ORDER_AGENT, "COLLECT_SELLER_EVIDENCE",
                                     {"seller_ids": late_sellers})
            await self._await(seller_task, self.order_agent.collect_sellers(seller_task))

        rules: PolicyRules | None = None
        if finding.issue != "insufficient_evidence" and self.policy_agent.has_tools():
            policy_task = bus.assign(COORDINATOR, POLICY_AGENT, "RESOLVE_POLICY",
                                     {"primary_issue": finding.issue, "context": context,
                                      "categories": sorted(intake.categories)})
            result = await self._await(
                policy_task, self.policy_agent.run(policy_task, finding, recipient=VERIFIER)
            )
            if isinstance(result, tuple):
                _, rules = result
        else:
            # No policy lookup possible: the decision still passes through the policy gate.
            self.trace.emit(case_id=intake.case_id, event_type="policy_decided",
                            actor=POLICY_AGENT, decision_code=finding.issue,
                            attributes={"policy_source": "default",
                                        "case_status": finding.status})

        verify_task = bus.assign(COORDINATOR, VERIFIER, "VERIFY_OUTPUT",
                                 {"primary_issue": finding.issue})
        output = build_output(intake, finding, analysis, self.store, rules)
        output, repairs = verify(output, self.store, self.trace.contracts)
        self.trace.emit(
            case_id=intake.case_id,
            event_type="verification_completed",
            actor=VERIFIER,
            decision_code="PASS_WITH_REPAIRS" if repairs else "PASS",
            evidence_refs=output["evidence_refs"][:20] or None,
            attributes={
                "primary_issue": output["assessment"]["primary_issue"],
                "case_status": output["assessment"]["case_status"],
                "repairs": ",".join(repairs)[:200] or None,
                "evidence_count": len(output["evidence_refs"]),
                "tool_failures": len(self.store.failures),
            },
        )
        bus.reply(verify_task, "OUTPUT_VERIFIED",
                  {"primary_issue": output["assessment"]["primary_issue"]},
                  output["evidence_refs"])
        return output


async def solve_case(
    case: dict[str, Any],
    gateway: Gateway,
    trace: TraceWriter,
    *,
    debug_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the coordinator -> specialists -> policy -> verifier workflow for one case."""
    intake = parse_intake(case)
    router = ToolRouter(await gateway.describe_tools())
    store = EvidenceStore(intake.case_id, gateway, trace)
    bus = A2ABus(intake.case_id, trace)
    try:
        return await Coordinator(intake, router, store, bus, trace).run()
    finally:
        if debug_dir is not None:
            store.dump(debug_dir)
