"""Unit tests for the offline rule layer.

Each test feeds a small synthetic document through the rules and checks that
key risk behaviours (flags and risk levels) fire.  These encode the "risk
marking" quality bar of the SPEC.
"""

from __future__ import annotations

from contract_extract.parsers import Document, Page
from contract_extract.rules.engine import run_rules


def _doc(*sentences: str) -> Document:
    text = "\n".join(sentences)
    return Document("synthetic", "docx", [Page(text, 0, 1)])


def _clauses(*sentences):
    flat: list[str] = []
    for s in sentences:
        if isinstance(s, (list, tuple)):
            flat.extend(s)
        else:
            flat.append(s)
    return run_rules(_doc(*flat))


def _risk_by_rule(sentences, rule: str):
    return {c.flag for c in _clauses(*sentences) if c.clause == rule}


def test_penalty_30_percent_is_high():
    flags = _risk_by_rule(
        ["In event of breach, Licensee shall pay Liquidated Damages of 30% of the total fees."],
        "penalty",
    )
    assert any("30%" in f and "onerous" in f for f in flags)


def test_penalty_is_capped_downgrades():
    clauses = [c for c in _clauses(
        ["Liquidated damages are capped at 2% of the contract value."]
    ) if c.clause == "penalty"]
    assert clauses and all(c.risk.value == "MEDIUM" for c in clauses)


def test_duration_over_five_years_is_high():
    flags = _risk_by_rule(["The initial term is 7 years from the effective date."], "duration")
    assert any("(long fix-term >5y)" in f for f in flags)


def test_automatic_renewal_is_medium():
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and any("auto-renew" in (c.flag or "") for c in clauses)
    assert all(c.risk.value == "MEDIUM" for c in clauses)


def test_illustrative_duration_not_fired():
    # "For example, a period of 3 weeks may be reasonable" is illustrative,
    # not a binding term.
    clauses = [c for c in _clauses(
        ["For example, a period of 3 weeks may be reasonable under the circumstances."]
    ) if c.clause == "duration"]
    assert clauses == []


def test_one_way_ip_assignment_is_high():
    clauses = [c for c in _clauses(
        ["All works made for hire and related intellectual property shall vest in Licensor."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_ip_conditional_assignment_is_medium():
    clauses = [c for c in _clauses(
        ["All intellectual property created by Provider shall be owned by Client upon full payment."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_broad_disclaimer_is_high():
    clauses = [c for c in _clauses(
        ["The Licensor disclaims all warranties and shall not be liable for any damages whatsoever."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_consequential_exclusion_is_low():
    clauses = [c for c in _clauses(
        ["Neither party shall be liable for indirect, incidental or consequential damages."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_gdpr_mention_is_low():
    clauses = [c for c in _clauses(
        ["The parties agree that all personal data shall be processed in accordance with GDPR."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_negated_confidentiality_not_fired():
    clauses = [c for c in _clauses(
        ["No confidentiality obligation applies to this facility."]
    ) if c.clause == "confidentiality"]
    assert clauses == []


def test_non_exclusive_not_fired():
    clauses = [c for c in _clauses(
        ["Independent Contractor status - no exclusivity is granted."]
    ) if c.clause == "exclusivity"]
    assert clauses == []


def test_negated_penalty_not_fired():
    clauses = [c for c in _clauses(
        ["No penalty shall apply in the event of delay."]
    ) if c.clause == "penalty"]
    assert clauses == []


def test_negated_subject_to_penalty_not_fired():
    clauses = [c for c in _clauses(
        ["Any party failing to notify the other within 10 days shall not be subject to any penalty."]
    ) if c.clause == "penalty"]
    assert clauses == []


def test_penalty_cap_not_treated_as_negation():
    # "No penalty shall exceed X" is a cap, not a negation: the penalty is
    # still risk-relevant and must fire.
    clauses = [c for c in _clauses(
        ["No penalty shall exceed 5% of the contract price."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_percent_of_fees_not_a_price():
    # A bare "N% of <money kind>" is a rate/share (late-payment rate, retention,
    # commission), not the price itself — the price rule must not fire.
    clauses = [c for c in _clauses(
        ["Liability for late payment is 5% of the annual fees."]
    ) if c.clause == "price"]
    assert clauses == []


def test_insurance_requirement_not_a_liability_cap():
    # "Professional indemnity insurance" is a coverage obligation (handled by
    # rule_insurance), not a party-to-party liability cap — the liability rule
    # must not fire, but a genuine indemnity obligation still does.
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "liability_cap"]
    assert clauses == []
    clauses = [c for c in _clauses(
        ["Provider indemnifies Client against all claims arising from staff misconduct."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_exclusive_5_years_is_high():
    clauses = [c for c in _clauses(
        ["This Agreement includes an exclusive license grant to Licensee for a period of 6 years."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "HIGH"
