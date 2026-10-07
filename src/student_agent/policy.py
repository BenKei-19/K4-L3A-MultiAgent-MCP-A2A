"""Interpret policy evidence and apply it to a draft finding.

Policy payloads are free-form; only entries that are explicitly about the chosen issue
(by key, by an issue/code field, or because the tool was queried with that issue) are
applied. Without a matching entry the rule-based defaults in ``decision`` stand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .decision import Finding, RefundLine
from .evidence import Evidence
from .facts import iter_dicts, norm_key, to_money, to_text

ACTION_KEYS = {"resolutionactions", "actions", "requiredactions", "recommendedactions",
               "action", "nextactions", "allowedactions"}
BASIS_KEYS = {"refund", "refundbasis", "refundrule", "compensation", "compensationbasis",
              "refundpolicy", "refundtype", "refundmode", "remedy"}
RATE_KEYS = {"refundrate", "refundpct", "refundpercent", "refundpercentage", "compensationrate",
             "compensationpct", "compensationpercent", "percentage", "rate", "pct"}
FIXED_KEYS = {"refundamountbrl", "compensationbrl", "compensationamountbrl", "fixedamountbrl",
              "voucherbrl", "voucheramountbrl", "creditbrl"}
PARTY_KEYS = {"responsibleparty", "liableparty", "partytype", "responsiblepartytype"}
STATUS_KEYS = {"casestatus"}
ISSUE_KEYS = {"issue", "primaryissue", "issuecode", "code", "scenario", "appliesto", "issuetype",
              "policycode", "type", "name", "category", "issues"}
PARTY_TYPES = {"seller", "platform", "logistics_provider", "payment_provider", "customer",
               "unknown"}
CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}


@dataclass
class PolicyRules:
    consulted: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    actions: list[str] | None = None
    basis: str | None = None
    rate: float | None = None
    fixed: float | None = None
    party: str | None = None
    status: str | None = None

    @property
    def source(self) -> str:
        return "mcp" if self.matched else "default"


def _values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _about(path: tuple[str, ...], record: dict[str, Any], issue: str) -> bool:
    target = norm_key(issue)
    if path and norm_key(path[-1]) == target:
        return True
    return any(
        norm_key(key) in ISSUE_KEYS and any(norm_key(str(v)) == target for v in _values(value))
        for key, value in record.items()
    )


def _queried_for(evidence: Evidence, issue: str) -> bool:
    return any(norm_key(str(value)) == norm_key(issue) for value in evidence.args.values())


def interpret_policy(evidence: list[Evidence], issue: str) -> PolicyRules:
    rules = PolicyRules()
    for item in evidence:
        rules.consulted.append(item.ref)
        records = list(iter_dicts(item.data))
        target = next((record for path, record in records if _about(path, record, issue)), None)
        if target is None and records and _queried_for(item, issue):
            # The tool was queried for this issue, so its top-level payload is the policy.
            target = records[0][1]
        if target is None:
            continue
        rules.matched.append(item.ref)
        for key, value in target.items():
            normalized = norm_key(key)
            if normalized in ACTION_KEYS:
                actions = [text for v in _values(value) if (text := to_text(v))]
                rules.actions = actions or rules.actions
            elif normalized in BASIS_KEYS and to_text(value):
                rules.basis = norm_key(to_text(value) or "")
            elif normalized in RATE_KEYS and to_money(value) is not None:
                rate = to_money(value) or 0.0
                rules.rate = rate / 100 if rate > 1 else rate
            elif normalized in FIXED_KEYS and to_money(value) is not None:
                rules.fixed = to_money(value)
            elif normalized in PARTY_KEYS and to_text(value) in PARTY_TYPES:
                rules.party = to_text(value)
            elif normalized in STATUS_KEYS and to_text(value) in CASE_STATUSES:
                rules.status = to_text(value)
    return rules


def _basis_amount(basis: str, bases: dict[str, float]) -> float | None:
    if any(word in basis for word in ("none", "norefund", "notapplicable", "noaction")):
        return 0.0
    for words, key in (
        (("freight", "shipping"), "freight"),
        (("duplicate",), "duplicate"),
        (("excess", "difference", "overcharge"), "excess"),
        (("pending",), "pending"),
        (("failed",), "failed"),
        (("full", "total", "outstanding", "captured", "orderamount", "ordervalue"), "outstanding"),
    ):
        if any(word in basis for word in words) and key in bases:
            return bases[key]
    return None


def apply_policy(finding: Finding, rules: PolicyRules) -> Finding:
    if not rules.matched:
        return finding
    if rules.status:
        finding.status = rules.status
    if rules.party and all(party != rules.party for party, _ in finding.parties):
        finding.parties = [(rules.party, None)]
    amount: float | None = None
    base_key = "outstanding" if "outstanding" in finding.bases else next(iter(finding.bases), None)
    if rules.fixed is not None:
        amount = rules.fixed
    elif rules.basis:
        amount = _basis_amount(rules.basis, finding.bases)
    if rules.rate is not None:
        base = amount if amount is not None else finding.bases.get(
            "items_total" if finding.issue.startswith("late_delivery") else base_key or "", 0.0
        )
        amount = round(base * rules.rate, 2)
    if amount is not None:
        template = finding.refund_lines[0] if finding.refund_lines else None
        reason = template.reason_code if template else f"{finding.issue.upper()}_REFUND"
        entity = template.entity_id if template else None
        finding.refund_lines = [RefundLine(reason, round(amount, 2), entity)] if amount > 0 else []
    if rules.actions:
        finding.actions = rules.actions
    return finding
