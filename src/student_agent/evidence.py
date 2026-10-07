"""Case-scoped evidence store and discovery-driven tool routing.

Tool names are never hard-coded: each discovered tool is mapped to a domain from its
name/description, and its arguments are filled only from identifiers that are already
known for this case. A required argument that cannot be filled means the tool is skipped.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .facts import norm_key
from .mcp_gateway import ToolCallError, ToolSpec
from .trace import TraceWriter

DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("policy", ("policy", "policies", "rule", "rules", "sla", "guideline", "guidelines")),
    ("refund", ("refund", "refunds", "chargeback", "reversal", "reversals")),
    ("payment", ("payment", "payments", "charge", "charges", "transaction", "transactions",
                 "billing", "capture", "captures")),
    ("shipment", ("shipment", "shipments", "shipping", "delivery", "deliveries", "tracking",
                  "logistics", "carrier", "fulfillment")),
    ("seller", ("seller", "sellers", "merchant", "merchants", "vendor", "vendors")),
    ("item", ("item", "items", "line", "lines")),
    ("product", ("product", "products", "catalog")),
    ("customer", ("customer", "customers", "buyer", "buyers")),
    ("order", ("order", "orders")),
)
PARAM_ENTITY = {
    "orderid": "order_id", "order": "order_id", "orderref": "order_id",
    "orderreference": "order_id",
    "sellerid": "seller_id", "seller": "seller_id", "merchantid": "seller_id",
    "shipmentid": "shipment_id", "shipment": "shipment_id", "trackingid": "shipment_id",
    "trackingnumber": "shipment_id",
    "paymentreference": "payment_reference", "paymentref": "payment_reference",
    "paymentid": "payment_reference", "transactionid": "payment_reference",
    "itemid": "item_id", "orderitemid": "item_id",
    "productid": "product_id",
    "refundid": "refund_id",
    "customerid": "customer_id", "customeruniqueid": "customer_id",
}
TOPIC_PARAMS = {"topic", "issue", "issuetype", "issuecode", "category", "policytype", "policyid",
                "policycode", "policyname", "claimtype", "reasoncode", "scenario", "type", "code",
                "name", "key", "query", "primaryissue", "casetype"}
MAX_FANOUT = 5
FAILURE_NOT_FOUND = "NOT_FOUND"
FAILURE_TIMEOUT = "MCP_TIMEOUT"
FAILURE_INVALID = "INVALID_RESPONSE"
FAILURE_TOOL = "TOOL_ERROR"
FAILURE_BUDGET = "CALL_BUDGET_EXHAUSTED"


class Gateway(Protocol):
    async def describe_tools(self) -> list[ToolSpec]: ...

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]: ...


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]


def tool_domain(spec: ToolSpec) -> str | None:
    for source in (_tokens(spec.name), _tokens(spec.description)):
        for domain, keywords in DOMAIN_KEYWORDS:
            if any(token in keywords for token in source):
                return domain
    return None


def choose_topic(options: tuple[str, ...] | None, hints: list[str]) -> str | None:
    if not hints:
        return None
    if not options:
        return hints[0]
    hint_tokens = [set(_tokens(hint)) for hint in hints]
    best, best_score = None, 0.0
    for option in options:
        option_tokens = set(_tokens(option))
        for rank, tokens in enumerate(hint_tokens):
            if not option_tokens or not tokens:
                continue
            exact = norm_key(option) == norm_key(hints[rank])
            score = (10 if exact else len(option_tokens & tokens) / len(option_tokens)) - rank / 10
            if score > best_score:
                best, best_score = option, score
    return best


class ToolRouter:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self.specs = specs
        self.domains = {spec.name: tool_domain(spec) for spec in specs}

    def tools_for(self, domains: set[str]) -> list[ToolSpec]:
        return [spec for spec in self.specs if self.domains[spec.name] in domains]

    def plan_calls(
        self, spec: ToolSpec, context: dict[str, list[str]], hints: list[str]
    ) -> list[dict[str, Any]]:
        options: list[list[tuple[str, Any]]] = []
        for param in spec.params:
            normalized = norm_key(param)
            if normalized == "caseid":
                continue
            required = param in spec.required
            entity = PARAM_ENTITY.get(normalized)
            if entity:
                values = context.get(entity, [])
                if not values or (not required and len(values) > 1):
                    if required:
                        return []
                    continue
                options.append([(param, value) for value in values[:MAX_FANOUT]])
            elif normalized in TOPIC_PARAMS or param in spec.enums:
                value = choose_topic(spec.enums.get(param), hints)
                if value is None or (not required and param not in spec.enums):
                    if required:
                        return []
                    continue
                options.append([(param, value)])
            elif required:
                return []
        return [dict(combo) for combo in itertools.islice(itertools.product(*options), MAX_FANOUT)]


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    domain: str
    data: Any
    actor: str
    args: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolFailure:
    actor: str
    tool: str
    code: str


@dataclass
class EvidenceStore:
    case_id: str
    gateway: Gateway
    trace: TraceWriter
    max_calls: int = 30
    items: list[Evidence] = field(default_factory=list)
    failures: list[ToolFailure] = field(default_factory=list)
    _calls: dict[tuple[str, str], Evidence | None] = field(default_factory=dict)

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> tuple[str, str]:
        return tool, json.dumps(args, sort_keys=True, default=str)

    def called(self, tool: str, args: dict[str, Any]) -> bool:
        return self._key(tool, args) in self._calls

    @property
    def refs(self) -> set[str]:
        return {evidence.ref for evidence in self.items}

    def by_domain(self, *domains: str) -> list[Evidence]:
        return [evidence for evidence in self.items if evidence.domain in domains]

    async def fetch(self, actor: str, spec: ToolSpec, args: dict[str, Any]) -> Evidence | None:
        key = self._key(spec.name, args)
        if key in self._calls:
            return self._calls[key]
        if len(self._calls) >= self.max_calls:
            self.failures.append(ToolFailure(actor, spec.name, FAILURE_BUDGET))
            return None
        self._calls[key] = None
        try:
            response = await self.gateway.call(spec.name, case_id=self.case_id, **args)
        except ToolCallError as exc:
            text = str(exc).lower()
            code = FAILURE_NOT_FOUND if "not found" in text or "unknown" in text else FAILURE_TOOL
            self.failures.append(ToolFailure(actor, spec.name, code))
            return None
        except ValueError:
            self.failures.append(ToolFailure(actor, spec.name, FAILURE_INVALID))
            return None
        except (TimeoutError, OSError):
            self.failures.append(ToolFailure(actor, spec.name, FAILURE_TIMEOUT))
            return None
        except Exception as exc:  # noqa: BLE001 - transport/protocol errors must not abort a case
            code = FAILURE_TIMEOUT if "timeout" in type(exc).__name__.lower() else FAILURE_TOOL
            self.failures.append(ToolFailure(actor, spec.name, code))
            return None
        evidence = Evidence(
            ref=response["evidence_ref"],
            tool=spec.name,
            domain=response["domain"],
            data=response["data"],
            actor=actor,
            args=dict(args),
            warnings=tuple(response.get("warnings") or ()),
        )
        self._calls[key] = evidence
        self.items.append(evidence)
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=spec.name,
            evidence_refs=[evidence.ref],
            attributes={"domain": evidence.domain, "warnings": len(evidence.warnings)},
        )
        return evidence

    def dump(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "case_id": self.case_id,
            "evidence": [
                {"tool": e.tool, "args": e.args, "domain": e.domain, "evidence_ref": e.ref,
                 "actor": e.actor, "warnings": list(e.warnings), "data": e.data}
                for e in self.items
            ],
            "failures": [f.__dict__ for f in self.failures],
        }
        (directory / f"{self.case_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
