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


def test_damages_not_a_penalty_boilerplate_not_fired():
    # "the sums are liquidated damages and not a penalty" is boilerplate
    # characterization with no amount or rate — not a penalty clause.
    clauses = [c for c in _clauses(
        ["The sums payable under this clause are liquidated damages and not a penalty."]
    ) if c.clause == "penalty"]
    assert clauses == []
    # A quantified liquidated-damages amount still fires.
    clauses = [c for c in _clauses(
        ["Buyer shall pay 10% of the fees as liquidated damages and not as a penalty."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "HIGH"


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


def test_renewable_energy_not_a_renewal():
    # "renewable energy sources" is a resource adjective, not a contract
    # renewal provision.
    clauses = [c for c in _clauses(
        ["The facility shall be powered by renewable energy sources."]
    ) if c.clause == "renewal"]
    assert clauses == []


def test_denied_right_to_renew_not_fired():
    # An express denial of a renewal right is a non-renewal statement, not a
    # renewal risk.
    clauses = [c for c in _clauses(
        ["Licensee shall have no right to renew this Agreement."]
    ) if c.clause == "renewal"]
    assert clauses == []


def test_exclusive_of_tax_not_an_exclusivity_grant():
    # "exclusive of VAT" means "excluding VAT", not a market-exclusivity grant.
    clauses = [c for c in _clauses(
        ["The price is exclusive of VAT and other taxes."]
    ) if c.clause == "exclusivity"]
    assert clauses == []


def test_exclusive_jurisdiction_not_an_exclusivity_grant():
    # "exclusive jurisdiction" is a forum-selection clause, not market
    # exclusivity.
    clauses = [c for c in _clauses(
        ["The courts of England shall have exclusive jurisdiction."]
    ) if c.clause == "exclusivity"]
    assert clauses == []


def test_licensed_premises_not_an_ip_clause():
    # "licensed premises" (alcohol / gaming permits) licenses a place, not IP.
    clauses = [c for c in _clauses(
        ["Sale of alcohol shall be confined to the licensed premises."]
    ) if c.clause == "ip"]
    assert clauses == []


def test_transfer_of_shares_not_a_data_transfer():
    # "transfer of shares" is a corporate/asset transfer, not personal data.
    clauses = [c for c in _clauses(
        ["The transfer of shares requires board approval."]
    ) if c.clause == "data_protection"]
    assert clauses == []


def test_transfer_of_business_or_employees_not_a_data_transfer():
    # "transfer of the business / employees / project" is a business or asset
    # transfer, not personal data.
    for sentence in [
        "The transfer of the business shall be completed within 60 days of signing.",
        "The transfer of employees to the purchaser shall occur on closing.",
        "The transfer of the project to the new operator requires board approval.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine cross-border personal-data transfer still fires.
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_no_acceptance_inspection_required_not_fired():
    # A negated acceptance requirement makes the machinery optional — not a
    # clause.
    clauses = [c for c in _clauses(
        ["No acceptance inspection is required for packaging materials."]
    ) if c.clause == "acceptance"]
    assert clauses == []


def test_subprovision_sunset_not_a_termination_right():
    # "This clause shall terminate upon expiry of the warranty" is a schedule
    # detail, not a party's right to terminate the agreement.
    clauses = [c for c in _clauses(
        ["This clause shall terminate upon the expiry of the warranty."]
    ) if c.clause == "termination"]
    assert clauses == []


def test_agreement_termination_still_fires():
    # The sunset guard must not swallow genuine agreement-level termination.
    clauses = [c for c in _clauses(
        ["This Agreement shall terminate on the expiry of the sponsorship period."]
    ) if c.clause == "termination"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_cancelled_without_penalty_not_fired():
    # "may be cancelled without penalty" denies a penalty explicitly, the same
    # way "no penalty shall apply" does.
    clauses = [c for c in _clauses(
        ["Up to 3 events may be cancelled without penalty."]
    ) if c.clause == "penalty"]
    assert clauses == []


def test_deposit_as_placement_verb_not_a_payment_term():
    # "shall deposit the goods at a warehouse" is placement, not payment.
    clauses = [c for c in _clauses(
        ["Buyer shall deposit the goods at the appointed warehouse within 5 days of arrival."]
    ) if c.clause == "payment"]
    assert clauses == []
    # The noun form "Security deposit of USD 5,000" is still a payment term.
    clauses = [c for c in _clauses(
        ["The security deposit of USD 5,000 shall be refunded within 30 days of expiry."]
    ) if c.clause == "payment"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_legal_process_delivery_not_a_delivery_obligation():
    # "deliver judgment/award" is a procedural deadline, not a goods/services
    # delivery obligation.
    clauses = [c for c in _clauses(
        ["The court shall deliver its judgment within 3 months of the final hearing."]
    ) if c.clause == "delivery"]
    assert clauses == []
    clauses = [c for c in _clauses(
        ["The arbitral award shall be delivered within 30 days of the hearing."]
    ) if c.clause == "delivery"]
    assert clauses == []
    # Genuine goods delivery still fires.
    clauses = [c for c in _clauses(
        ["Seller shall deliver the goods within 30 days of the purchase order."]
    ) if c.clause == "delivery"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_notice_report_delivery_not_a_delivery_obligation():
    # "delivery of this notice / the report delivery" is a procedural
    # communication deadline, not a goods/services delivery obligation.
    for sentence in [
        "Delivery of this notice shall be within 3 days of the event.",
        "The report delivery shall be within 5 days of the board meeting.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "delivery"]
        assert clauses == [], sentence
    # Genuine goods delivery still fires.
    clauses = [c for c in _clauses(
        ["Seller shall deliver the goods within 30 days of the purchase order."]
    ) if c.clause == "delivery"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_total_of_business_days_not_a_quantity():
    # "a total of N business/calendar days" is a procedural time period (review
    # period, response window), not a quantity of deliverables.
    for sentence in [
        "The review period is a total of 5 business days from the request.",
        "A total of 30 calendar days is allowed for the appeal window.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # Countable deliverables still fire.
    clauses = [c for c in _clauses(
        ["Buyer shall purchase a total of 10,000 units within the term."]
    ) if c.clause == "quantity"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_negated_interest_accrual_not_a_penalty():
    # "interest shall not accrue" / "no interest will accrue" states what will
    # not happen — not a penalty risk.
    for sentence in [
        "Interest shall not accrue on any late payments under this Agreement.",
        "No interest will accrue on overdue invoices.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine accrual clause still fires.
    clauses = [c for c in _clauses(
        ["In the event of late payment, interest of 2% per month shall accrue."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_force_majeure_not_a_liability_cap():
    # A force-majeure excuse and an own-taxes allocation are not party-to-party
    # liability caps.
    for sentence in [
        "Neither party shall be liable for damages caused by a force majeure event.",
        "Each party shall be liable for its own taxes and duties under this Agreement.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine disclaimer still fires.
    clauses = [c for c in _clauses(
        ["The Licensor disclaims all warranties and shall not be liable for any damages whatsoever."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_personnel_assignment_not_an_ip_clause():
    # "project manager assigned to the account / assigned personnel" allocates
    # staff, not intellectual property.
    for sentence in [
        "The project manager assigned to the account shall report weekly.",
        "Two staff members assigned to the task shall be seconded.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # A genuine IP assignment still fires.
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_exclusive_remedy_or_use_not_an_exclusivity_grant():
    # "exclusive remedy for a breach" / "exclusive use of the premises" are a
    # remedy and a physical right, not a market-exclusivity term.
    for sentence in [
        "Termination is the exclusive remedy for any breach of this Agreement.",
        "Tenant shall have exclusive use of the premises during the term.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    # An exclusive license grant still fires.
    clauses = [c for c in _clauses(
        ["This Agreement includes an exclusive license grant to Licensee for a period of 6 years."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_negated_insurance_requirement_not_fired():
    # "nothing requires either party to maintain insurance" / "does not include
    # liability insurance" deny a coverage obligation — no clause.
    for sentence in [
        "Nothing in this Agreement requires either party to maintain any insurance.",
        "The Service does not include liability insurance of any kind.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_permit_or_licence_renewal_not_a_contract_renewal():
    # "apply for renewal of the licence/permit/visa" is an administrative
    # re-issuance of a credential, not a contract renewal provision.
    for sentence in [
        "The holder may apply for renewal of the licence by submitting a fee.",
        "The applicant may apply for renewal of the permit annually.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # A genuine automatic-renewal provision still fires.
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_court_order_term_not_a_contract_duration():
    # "the term of the court order / the duration of the injunction" is a legal
    # instrument's effective period, not a commercial contract term.
    for sentence in [
        "The term of the court order is 3 years.",
        "The duration of the injunction is 2 years.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term still fires.
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_banking_acceptance_not_a_deliverables_clause():
    # "accept the documentary credit / bill of exchange / offer / tender" is a
    # trade-finance or formation acceptance, not acceptance of deliverables.
    for sentence in [
        "The bank shall accept the documentary credit within 7 days of presentation.",
        "The bank shall accept the bill of exchange upon presentation by the seller.",
        "Buyer accepts the offer set out in the quotation of 15 May.",
        "The customer may accept a tender submitted by any qualified bidder.",
        "Acceptance of the offer shall be communicated in writing within 10 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    # A genuine deliverables acceptance window still fires.
    clauses = [c for c in _clauses(
        ["The Buyer shall have 10 days to inspect and accept each deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"
