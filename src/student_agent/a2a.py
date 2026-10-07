"""Minimal in-process A2A protocol: typed envelopes correlated by case_id.

Every assignment and handoff is mirrored to the observable trace. Only decision
codes and evidence refs are traced, never free-form reasoning.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"


class HopLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class A2AMessage:
    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    status: str = "ok"
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(8)}")
    reply_to: str | None = None


class A2ABus:
    """Routes envelopes for one case. A hop budget prevents delegation loops."""

    def __init__(self, case_id: str, trace: TraceWriter, max_hops: int = 16) -> None:
        self.case_id = case_id
        self.trace = trace
        self.max_hops = max_hops
        self.hops = 0

    def _count_hop(self) -> None:
        self.hops += 1
        if self.hops > self.max_hops:
            raise HopLimitExceeded(f"{self.case_id}: A2A hop budget exhausted")

    def assign(
        self, sender: str, recipient: str, intent: str, payload: dict[str, Any] | None = None
    ) -> A2AMessage:
        self._count_hop()
        message = A2AMessage(self.case_id, sender, recipient, intent, payload or {})
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=intent,
            attributes={"message_id": message.message_id, "hop": self.hops},
        )
        return message

    def reply(
        self,
        request: A2AMessage,
        intent: str,
        payload: dict[str, Any],
        evidence_refs: list[str] | tuple[str, ...] = (),
        status: str = "ok",
        recipient: str | None = None,
    ) -> A2AMessage:
        self._count_hop()
        message = A2AMessage(
            case_id=self.case_id,
            sender=request.recipient,
            recipient=recipient or request.sender,
            intent=intent,
            payload=payload,
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            status=status,
            reply_to=request.message_id,
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=message.sender,
            target=message.recipient,
            decision_code=intent,
            evidence_refs=list(message.evidence_refs[:20]) or None,
            attributes={
                "message_id": message.message_id,
                "reply_to": request.message_id,
                "status": status,
                "hop": self.hops,
            },
        )
        return message
