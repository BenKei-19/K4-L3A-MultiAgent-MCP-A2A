"""Assemble the L3A output and enforce cross-field invariants before finalize."""

from __future__ import annotations

from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .contracts import Contracts
from .decision import CITABLE_DOMAINS, Analysis, Finding
from .evidence import EvidenceStore
from .intake import PAYMENT_CATEGORIES, Intake, issues_for
from .policy import PolicyRules

CLAIM_DOMAINS = {
    "late_delivery": {"order", "shipment", "item"},
    **{category: {"order", "payment", "refund", "item"} for category in PAYMENT_CATEGORIES},
}
REFUND_ACTION_WORDS = ("refund", "compensat", "reissue", "retry", "credit", "reimburs")


def _unique(values: list[Any], limit: int) -> list[Any]:
    return list(dict.fromkeys(v for v in values if v not in (None, "")))[:limit]


def select_refs(finding: Finding, store: EvidenceStore, rules: PolicyRules | None) -> list[str]:
    domains = finding.domains & CITABLE_DOMAINS
    refs = [evidence.ref for evidence in store.items if evidence.domain in domains]
    if rules and finding.status != "no_action":
        refs += rules.matched or rules.consulted
    return _unique(refs, 30)


def entities(analysis: Analysis) -> dict[str, list[str]]:
    facts = analysis.facts
    return {
        "order_ids": _unique([o.order_id for o in facts.orders], 20),
        "item_ids": sorted(_unique([i.item_id for i in facts.items], 20)),
        "seller_ids": _unique(facts.seller_ids(), 20),
        "payment_references": sorted(_unique([p.reference for p in facts.payments], 20)),
        "shipment_ids": sorted(_unique([s.shipment_id for s in facts.shipments], 20)),
    }


def assess_claims(
    intake: Intake, finding: Finding, analysis: Analysis, store: EvidenceStore, cited: list[str]
) -> list[dict[str, Any]]:
    detected = {f.issue for f in analysis.detected}
    assessments = []
    for claim in intake.claims:
        if not claim.claim_id:
            continue
        families = issues_for(claim.categories)
        domains = set().union(*(CLAIM_DOMAINS.get(c, set()) for c in claim.categories)) or {
            "order"
        }
        refs = [e.ref for e in store.items if e.domain in domains and e.ref in cited]
        confidence = finding.confidence
        if finding.issue == "insufficient_evidence":
            verdict, confidence = "insufficient_evidence", min(confidence, 0.6)
        elif not claim.categories:
            verdict = "supported" if finding.status == "action_required" else "unsupported"
            confidence -= 0.15
            refs = list(cited)
        elif finding.issue in families and finding.issue != "valid_split_payment":
            subtype_differs = (
                ("duplicate_charge" in claim.categories and finding.issue == "payment_mismatch")
                or ("payment_mismatch" in claim.categories and finding.issue == "duplicate_charge")
            )
            verdict = "partially_supported" if subtype_differs else "supported"
        elif families & detected:
            verdict = "supported"
            confidence -= 0.1
        elif refs:
            verdict = "unsupported"
        else:
            verdict, confidence = "insufficient_evidence", min(confidence, 0.55)
        assessments.append({
            "claim_id": claim.claim_id[:64],
            "verdict": verdict,
            "confidence": round(min(0.95, max(0.3, confidence)), 2),
            "evidence_refs": _unique(refs, 30),
        })
    return assessments[:5]


def build_output(
    intake: Intake,
    finding: Finding,
    analysis: Analysis,
    store: EvidenceStore,
    rules: PolicyRules | None,
) -> dict[str, Any]:
    cited = select_refs(finding, store, rules)
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": intake.case_id,
        "assessment": {
            "primary_issue": finding.issue,
            "case_status": finding.status,
            "confidence": finding.confidence,
        },
        "affected_entities": entities(analysis),
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": code, "rank": rank}
                for rank, code in enumerate(_unique(finding.causes, 5), 1)
            ],
            "responsible_parties": [
                {"party_type": party_type, "party_id": party_id}
                for party_type, party_id in _unique(finding.parties, 5)
            ],
        },
        "evidence_refs": cited,
        "data_conflicts": _unique_conflicts(analysis.conflicts),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": finding.refund_total,
            "refund_lines": [
                {"reason_code": line.reason_code, "amount_brl": line.amount,
                 "entity_id": line.entity_id}
                for line in finding.refund_lines
            ],
        },
        "resolution_actions": _unique([a[:80] for a in finding.actions], 8),
    }
    claims = assess_claims(intake, finding, analysis, store, cited)
    if claims:
        output["claim_assessments"] = claims
    return output


def _unique_conflicts(conflicts: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for conflict in conflicts:
        seen.setdefault(conflict["field"], conflict)
    return list(seen.values())[:5]


def verify(
    output: dict[str, Any], store: EvidenceStore, contracts: Contracts
) -> tuple[dict[str, Any], list[str]]:
    """Check invariants, repair deterministically, and return the list of repairs made."""
    repairs: list[str] = []
    owned = store.refs
    assessment = output["assessment"]
    finance = output["financial_resolution"]

    refs = [ref for ref in output["evidence_refs"] if ref in owned]
    if refs != output["evidence_refs"]:
        repairs.append("EVIDENCE_OWNERSHIP")
    order_refs = [e.ref for e in store.by_domain("order")]
    if assessment["primary_issue"] != "insufficient_evidence" and order_refs and not (
        set(order_refs) & set(refs)
    ):
        refs = _unique(order_refs[:1] + refs, 30)
        repairs.append("ORDER_EVIDENCE_REQUIRED")
    output["evidence_refs"] = refs
    for claim in output.get("claim_assessments", []):
        linked = [ref for ref in claim["evidence_refs"] if ref in refs]
        if linked != claim["evidence_refs"]:
            claim["evidence_refs"] = linked
            repairs.append("CLAIM_LINKAGE")

    lines = finance["refund_lines"]
    for line in lines:
        line["amount_brl"] = round(max(0.0, float(line["amount_brl"])), 2)
    lines[:] = [line for line in lines if line["amount_brl"] > 0][:10]
    if assessment["case_status"] == "no_action" and (lines or output["resolution_actions"]):
        lines.clear()
        output["resolution_actions"] = []
        repairs.append("NO_ACTION_HAS_NO_REMEDY")
    total = round(sum(line["amount_brl"] for line in lines), 2)
    if total != finance["recommended_refund_brl"]:
        repairs.append("REFUND_TOTAL")
    finance["recommended_refund_brl"] = total

    actions = output["resolution_actions"]
    if total > 0 and assessment["case_status"] != "action_required":
        assessment["case_status"] = "action_required"
        repairs.append("REFUND_REQUIRES_ACTION")
    if total > 0 and not any(w in a.lower() for a in actions for w in REFUND_ACTION_WORDS):
        actions.insert(0, "refund_customer")
        repairs.append("REFUND_ACTION_MISSING")
    if assessment["case_status"] == "action_required" and not actions:
        actions.append("escalate_manual_review")
        repairs.append("ACTION_REQUIRED_HAS_ACTION")
    output["resolution_actions"] = _unique(actions, 8)

    sellers = output["affected_entities"]["seller_ids"]
    for party in output["root_cause_analysis"]["responsible_parties"]:
        seller_id = party["party_id"] if party["party_type"] == "seller" else None
        if seller_id and seller_id not in sellers:
            sellers.append(seller_id)
            repairs.append("SELLER_SCOPE")

    confidence = float(assessment["confidence"])
    if assessment["primary_issue"] == "insufficient_evidence":
        confidence = min(confidence, 0.8)
    bounded = round(min(1.0, max(0.0, confidence)), 2)
    if bounded != assessment["confidence"]:
        repairs.append("CONFIDENCE_BOUNDS")
    assessment["confidence"] = bounded

    contracts.validate_output(output, f"outputs/{output['case_id']}.json")
    return output, _unique(repairs, 20)
