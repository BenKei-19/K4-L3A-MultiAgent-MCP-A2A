from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections import Counter
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, ToolCallError, ToolSpec
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER = "a" * 32
SELLER_A = "s" * 32
SELLER_B = "t" * 32
ISSUES = [
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch", "duplicate_charge",
    "refund_pending", "refund_failed", "unsupported_claim", "insufficient_evidence",
]

STANDARD_TOOLS = {
    "order": "get_order", "item": "get_order_items", "payment": "get_payments",
    "refund": "get_refunds", "shipment": "get_shipment", "seller": "get_seller",
    "policy": "get_policy", "customer": "get_customer",
}
RENAMED_TOOLS = {
    "order": "fetch_order_record", "item": "list_order_lines", "payment": "list_transactions",
    "refund": "list_chargebacks", "shipment": "track_delivery", "seller": "lookup_merchant",
    "policy": "lookup_policy_rules", "customer": "get_buyer_profile",
}


def _order(status: str = "delivered", delivered: str | None = "2018-01-10 12:00:00",
           estimated: str = "2018-01-15 00:00:00", carrier: str | None = "2018-01-05 09:00:00"):
    return {
        "order_id": ORDER, "customer_id": "c" * 32, "order_status": status,
        "order_purchase_timestamp": "2018-01-01 10:00:00",
        "order_delivered_carrier_date": carrier,
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": estimated,
    }


def _items(limit: str = "2018-01-06 00:00:00", price: float = 85.0, freight: float = 15.0):
    return {"order_id": ORDER, "items": [{
        "item_id": f"{ORDER}-1", "order_item_id": 1, "product_id": "p" * 32,
        "seller_id": SELLER_A, "shipping_limit_date": limit, "price": price,
        "freight_value": freight,
    }]}


def _payments(*rows: tuple[str, float]):
    return {"order_id": ORDER, "payments": [
        {"payment_reference": f"PAY-{index}", "payment_sequential": index, "payment_type": kind,
         "payment_installments": 1, "payment_value": value, "status": "captured"}
        for index, (kind, value) in enumerate(rows, 1)
    ]}


def _refunds(*rows: tuple[float, str]):
    return {"order_id": ORDER, "refunds": [
        {"refund_id": f"RF-{index}", "payment_reference": "PAY-1", "amount_brl": amount,
         "status": status}
        for index, (amount, status) in enumerate(rows, 1)
    ]}


def _shipment(delivered: str | None = "2018-01-10 12:00:00", carrier: str = "2018-01-05 09:00:00"):
    return {"shipments": [{
        "shipment_id": "SHP-1", "order_id": ORDER, "seller_id": SELLER_A, "carrier_id": "CARRIER-9",
        "handed_to_carrier_at": carrier, "delivered_at": delivered,
        "estimated_delivery_date": "2018-01-15",
    }]}


SCENARIOS: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "message": "My order was canceled but I was still charged.",
        "data": {"order": _order("canceled", None, carrier=None), "item": _items(),
                 "payment": _payments(("credit_card", 100.0)), "refund": _refunds()},
        "refund": 100.0,
    },
    "unavailable_order_paid": {
        "message": "The product was unavailable but my card was charged.",
        "data": {"order": _order("unavailable", None, carrier=None), "item": _items(),
                 "payment": _payments(("credit_card", 100.0)), "refund": _refunds()},
        "refund": 100.0,
    },
    "late_delivery_seller": {
        "message": "Meu pedido chegou atrasado!",
        "data": {"order": _order(delivered="2018-01-20 12:00:00", carrier="2018-01-12 09:00:00"),
                 "item": _items(), "payment": _payments(("boleto", 100.0)), "refund": _refunds(),
                 "shipment": _shipment("2018-01-20 12:00:00", "2018-01-12 09:00:00")},
        "refund": 15.0,
    },
    "late_delivery_logistics": {
        "message": "Đơn hàng giao trễ quá, tôi rất thất vọng.",
        "data": {"order": _order(delivered="2018-01-20 12:00:00"), "item": _items(),
                 "payment": _payments(("boleto", 100.0)), "refund": _refunds(),
                 "shipment": _shipment("2018-01-20 12:00:00")},
        "refund": 15.0,
    },
    "valid_split_payment": {
        "message": "I was charged twice for the same order!",
        "data": {"order": _order(), "item": _items(), "refund": _refunds(),
                 "payment": _payments(("voucher", 30.0), ("credit_card", 70.0)),
                 "shipment": _shipment()},
        "refund": 0.0,
    },
    "payment_mismatch": {
        "message": "I was overcharged, the amount is wrong.",
        "data": {"order": _order(), "item": _items(), "refund": _refunds(),
                 "payment": _payments(("credit_card", 120.0)), "shipment": _shipment()},
        "refund": 20.0,
    },
    "duplicate_charge": {
        "message": "Fui cobrado duas vezes pelo mesmo pedido.",
        "data": {"order": _order(), "item": _items(), "refund": _refunds(),
                 "payment": _payments(("credit_card", 100.0), ("credit_card", 100.0)),
                 "shipment": _shipment()},
        "refund": 100.0,
    },
    "refund_pending": {
        "message": "Where is my refund? It has been weeks.",
        "data": {"order": _order("canceled", None, carrier=None), "item": _items(),
                 "payment": _payments(("credit_card", 100.0)),
                 "refund": _refunds((100.0, "pending"))},
        "refund": 100.0,
    },
    "refund_failed": {
        "message": "My refund never arrived.",
        "data": {"order": _order("canceled", None, carrier=None), "item": _items(),
                 "payment": _payments(("credit_card", 100.0)),
                 "refund": _refunds((100.0, "failed"))},
        "refund": 100.0,
    },
    "unsupported_claim": {
        "message": "My package arrived late.",
        "data": {"order": _order(), "item": _items(), "refund": _refunds(),
                 "payment": _payments(("credit_card", 100.0)), "shipment": _shipment()},
        "refund": 0.0,
    },
    "insufficient_evidence": {
        "message": "I have a problem with my order.",
        "data": {},
        "refund": 0.0,
    },
}


class FakeGateway:
    """In-memory MCP gateway that mimics evidence envelopes and records every call."""

    def __init__(self, data: dict[str, Any], names: dict[str, str]) -> None:
        self.data = data
        self.names = names
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.issued: dict[str, str] = {}
        by_param = {"seller": "seller_id", "policy": "issue_type"}
        self.specs = [
            ToolSpec(
                name=name,
                description=f"Return {domain} evidence for a case.",
                params=("case_id", by_param.get(domain, "order_id")),
                required=("case_id",) if domain == "policy"
                else ("case_id", by_param.get(domain, "order_id")),
                enums={"issue_type": tuple(ISSUES)} if domain == "policy" else {},
            )
            for domain, name in names.items()
        ]
        self.domain_of = {name: domain for domain, name in names.items()}

    async def describe_tools(self) -> list[ToolSpec]:
        return self.specs

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        domain = self.domain_of[tool_name]
        if domain == "policy":
            payload: Any = {"policies": [{"issue": arguments.get("issue_type"),
                                          "notes": "default policy"}]}
        elif domain == "seller":
            payload = {"seller_id": arguments["seller_id"], "seller_state": "SP"}
        elif domain == "customer":
            payload = {"customer_id": "c" * 32}
        else:
            if arguments.get("order_id") != ORDER or domain not in self.data:
                raise ToolCallError(f"MCP tool {tool_name} failed: order not found")
            payload = self.data[domain]
        ref = f"ev_{secrets.token_urlsafe(24)}"
        self.issued[ref] = case_id
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {"schema_version": "day09-mcp-evidence-v1", "evidence_ref": ref,
                "result_hash": f"sha256:{digest}", "domain": domain, "data": payload}


def _run(issue: str, tmp_path: Path, names: dict[str, str] = STANDARD_TOOLS):
    scenario = SCENARIOS[issue]
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(scenario["data"], names)
    case = {"case_id": "L3A_CASE_001", "order_id": ORDER, "customer_message": scenario["message"]}
    trace.emit(case_id="L3A_CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id="L3A_CASE_001", event_type="case_finalized", actor="coordinator")
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    contracts.validate_output(output, "output")
    return output, events, gateway


@pytest.mark.parametrize("issue", ISSUES)
def test_each_primary_issue_is_detected(issue: str, tmp_path: Path) -> None:
    output, _, _ = _run(issue, tmp_path)
    assert output["assessment"]["primary_issue"] == issue
    finance = output["financial_resolution"]
    assert finance["recommended_refund_brl"] == pytest.approx(SCENARIOS[issue]["refund"])
    assert finance["recommended_refund_brl"] == pytest.approx(
        sum(line["amount_brl"] for line in finance["refund_lines"])
    )


@pytest.mark.parametrize("issue", ISSUES)
def test_tool_names_come_from_discovery(issue: str, tmp_path: Path) -> None:
    output, _, gateway = _run(issue, tmp_path, RENAMED_TOOLS)
    assert output["assessment"]["primary_issue"] == issue
    assert {name for name, _ in gateway.calls} <= set(RENAMED_TOOLS.values())


@pytest.mark.parametrize("issue", ISSUES)
def test_trace_covers_workflow_and_links_evidence(issue: str, tmp_path: Path) -> None:
    output, events, gateway = _run(issue, tmp_path)
    kinds = [event["event_type"] for event in events]
    for required in ("case_received", "task_assigned", "handoff", "policy_decided",
                     "verification_completed", "case_finalized"):
        assert required in kinds
    assert kinds[0] == "case_received" and kinds[-1] == "case_finalized"
    assert len({event["actor"] for event in events}) >= 4
    consumed = {ref for event in events if event["event_type"] == "tool_result_consumed"
                for ref in event.get("evidence_refs", [])}
    assert set(output["evidence_refs"]) <= consumed
    assert set(output["evidence_refs"]) <= set(gateway.issued)


def test_consistency_rules_hold_for_all_scenarios(tmp_path: Path) -> None:
    for index, issue in enumerate(ISSUES):
        output, _, gateway = _run(issue, tmp_path / str(index))
        status = output["assessment"]["case_status"]
        refund = output["financial_resolution"]["recommended_refund_brl"]
        if status == "no_action":
            assert refund == 0 and output["resolution_actions"] == []
        if refund > 0:
            assert status == "action_required"
        if status == "action_required":
            assert output["resolution_actions"]
        sellers = set(output["affected_entities"]["seller_ids"])
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"]:
                assert party["party_id"] in sellers
        # Customer data is never needed for L3A conclusions.
        assert "get_customer" not in Counter(name for name, _ in gateway.calls)


def test_late_seller_names_the_late_seller(tmp_path: Path) -> None:
    output, _, _ = _run("late_delivery_seller", tmp_path)
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": SELLER_A}]


def test_valid_split_payment_rejects_double_charge_claim(tmp_path: Path) -> None:
    output, _, _ = _run("valid_split_payment", tmp_path)
    assert output["assessment"]["case_status"] == "no_action"
    assert output["affected_entities"]["payment_references"] == ["PAY-1", "PAY-2"]


def test_matching_policy_overrides_default_remedy(tmp_path: Path) -> None:
    class StrictPolicyGateway(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            response = await super().call(tool_name, case_id=case_id, **arguments)
            if self.domain_of[tool_name] == "policy":
                response["data"] = {"late_delivery_seller": {
                    "compensation": "none", "actions": ["warn_seller", "apologize_to_customer"],
                }}
            return response

    scenario = SCENARIOS["late_delivery_seller"]
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = StrictPolicyGateway(scenario["data"], STANDARD_TOOLS)
    case = {"case_id": "L3A_CASE_002", "order_id": ORDER, "customer_message": scenario["message"]}
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["resolution_actions"] == ["warn_seller", "apologize_to_customer"]
    assert output["assessment"]["case_status"] == "action_required"


def test_order_and_shipment_date_conflict_is_reported(tmp_path: Path) -> None:
    scenario = SCENARIOS["late_delivery_logistics"]
    data = dict(scenario["data"], order=_order(delivered="2018-01-12 12:00:00"))
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {"case_id": "L3A_CASE_003", "order_id": ORDER, "customer_message": scenario["message"]}
    output = asyncio.run(solve_case(case, FakeGateway(data, STANDARD_TOOLS), trace))
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["data_conflicts"] == [{
        "field": "delivered_customer_date", "sources": ["order", "shipment"],
        "selected_source": "shipment", "resolution_code": "PREFER_SHIPMENT_TRACKING",
    }]


def test_structured_claims_are_assessed(tmp_path: Path) -> None:
    scenario = SCENARIOS["valid_split_payment"]
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {"case_id": "L3A_CASE_004", "order_id": ORDER, "claims": [
        {"claim_id": "C1", "claim_type": "duplicate_charge", "text": "Charged twice"},
        {"claim_id": "C2", "claim_type": "late_delivery", "text": "It arrived late"},
    ]}
    output = asyncio.run(solve_case(case, FakeGateway(scenario["data"], STANDARD_TOOLS), trace))
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert verdicts == {"C1": "unsupported", "C2": "unsupported"}
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= set(output["evidence_refs"])


class _FakeSession:
    def __init__(self, result: mcp_types.CallToolResult) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        return self.result


def test_gateway_reports_tool_errors_with_mcp_v2_fields() -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    error = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="order not found")], is_error=True
    )
    gateway = EvidenceGateway(_FakeSession(error), contracts)  # type: ignore[arg-type]
    with pytest.raises(ToolCallError, match="not found"):
        asyncio.run(gateway.call("get_order", case_id="L3A_CASE_001", order_id=ORDER))


def test_gateway_reads_structured_evidence() -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    evidence = {"schema_version": "day09-mcp-evidence-v1", "evidence_ref": "ev_" + "x" * 24,
                "result_hash": "sha256:" + "0" * 64, "domain": "order", "data": {}}
    result = mcp_types.CallToolResult(content=[], structured_content=evidence)
    gateway = EvidenceGateway(_FakeSession(result), contracts)  # type: ignore[arg-type]
    assert asyncio.run(gateway.call("get_order", case_id="L3A_CASE_001")) == evidence
