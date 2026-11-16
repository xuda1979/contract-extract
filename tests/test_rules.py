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


def test_statutory_or_regulatory_penalty_not_a_contract_penalty():
    # "subject to prosecution and penalties under applicable law" and
    # "disciplinary penalties for breach of policy" are statutory or
    # authority-imposed sanctions, not contractual default penalties.
    for sentence in [
        "Any false claim is subject to prosecution and penalties under applicable law.",
        "The Employee shall be subject to disciplinary penalties for breach of policy.",
        "Fines imposed by the regulator shall be borne by the party at fault.",
        "A penalty may be imposed by the authority for late filing of the return.",
        "Conviction of fraud carries statutory penalties under the Commercial Code.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine contractual default penalty still fires.
    clauses = [c for c in _clauses(
        ["In the event of breach, the defaulting party shall pay a penalty of 1.5% per month of the invoice amount."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_denied_ip_transfer_not_fired():
    # "nothing grants any intellectual property rights", "does not grant any
    # rights", "no rights are transferred" state what will not happen — not an
    # IP ownership risk.
    for sentence in [
        "Nothing in this Agreement grants any intellectual property rights to either party.",
        "The Licensor does not grant any rights to the intellectual property.",
        "All intellectual property remains with its respective owner and no rights are transferred.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # A genuine IP transfer still fires.
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_negated_transfer_not_a_data_transfer():
    # "no rights are transferred / nothing transfers any rights" is an IP
    # ownership statement, not a personal-data transfer.
    for sentence in [
        "All intellectual property remains with its respective owner and no rights are transferred.",
        "Nothing in this Agreement transfers any rights to the purchaser.",
        "No ownership is transferred under this clause.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine cross-border personal-data transfer still fires.
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_domain_or_subscription_renewal_not_a_contract_renewal():
    # "renewal of the domain name registration / subscription / membership" is
    # an account or registration renewal, not a renewal provision for the
    # agreement itself.
    for sentence in [
        "Renewal of the domain name registration shall be the responsibility of the customer.",
        "Charges will be applied to any renewal of the subscription.",
        "The membership renews each year unless the member cancels.",
        "The parties shall pay the renewal fee for the permit before its expiry.",
        "Hosting services renew automatically each month unless cancelled.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # A genuine agreement-term renewal still fires.
    clauses = [c for c in _clauses(
        ["This SaaS Subscription renews automatically each month unless the customer cancels with notice of 30 days."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_insurance_policy_renewal_not_an_insurance_obligation():
    # "the insurance policy renews annually" is a policy-admin renewal mention,
    # not a coverage obligation (and not an agreement renewal either).
    for sentence in [
        "The insurance policy renews annually on the anniversary date.",
        "The policy may be renewed upon payment of the annual premium.",
        "The parties shall arrange renewal of the insurance policy before expiry.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause in ("insurance", "renewal")]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_quantity_detail_populated_with_real_counts():
    # The count-regex previously started with a bare alternation that matched
    # the empty string, so EVERY quantity hit emitted an empty quote/detail.
    # Procedural mentions ("against the presented documents") must not fire;
    # genuine countable deliveries must carry their real count as detail.
    clauses = [c for c in _clauses(
        ["The bank shall make payment against the presented documents."]
    ) if c.clause == "quantity"]
    assert clauses == []
    positive = [c for c in _clauses(
        ["A total of 10,000 units shall be delivered within 45 days."]
    ) if c.clause == "quantity"]
    assert positive and positive[0].detail == "10,000 units"
    pages = [c for c in _clauses(
        ["Each deliverable shall contain at least 50 pages of documentation."]
    ) if c.clause == "quantity"]
    assert pages and pages[0].detail == "50 pages"


def test_rate_less_loan_interest_not_a_penalty():
    # "the interest rate shall apply for the remaining period of the loan" is
    # loan pricing with no rate and no late/breach context — not a default
    # penalty.  Genuine late-payment accrual still fires.
    clauses = [c for c in _clauses(
        ["The interest rate shall apply for the remaining period of the loan."]
    ) if c.clause == "penalty"]
    assert clauses == []
    positive = [c for c in _clauses(
        ["Interest shall accrue on all late payments."]
    ) if c.clause == "penalty"]
    assert positive and positive[0].risk.value == "MEDIUM"


def test_survival_provision_not_a_termination_clause():
    # "confidentiality obligations survive the termination of this Agreement"
    # states what outlives the agreement — not a party's right to terminate it.
    for sentence in [
        "Confidentiality obligations survive the termination of this Agreement.",
        "Confidential obligations shall survive for 5 years following termination.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    # Genuine termination rights still fire.
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for convenience upon 90 days written notice."]
    ) if c.clause == "termination"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_insurance_as_required_by_law_not_a_requirement():
    # "insurance as required by applicable law" re-states a statutory duty
    # without a negotiated coverage type — boilerplate, not a coverage
    # obligation.
    for sentence in [
        "Each party shall maintain insurance coverage as required by applicable law.",
        "The parties shall carry liability insurance as required by law.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_confidentiality_not_apply_carveout_suppressed():
    # "confidentiality shall not apply to publicly available information" is
    # a carve-out stating what is excluded — not a confidentiality obligation.
    for sentence in [
        "Confidentiality shall not apply to information that is publicly available.",
        "The confidentiality obligations shall not apply to information already in the public domain.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    # Genuine confidentiality obligations still fire.
    clauses = [c for c in _clauses(
        ["Confidential information shall remain confidential for 3 years after termination."]
    ) if c.clause == "confidentiality"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_data_protection_generic_compliance_and_dpo_contact_suppressed():
    # "comply with all applicable data protection laws" is a generic legal
    # promise with no processing / transfer / breach obligation, and a
    # "data protection officer contact details:" line is a label — neither
    # is a working data-protection clause.
    for sentence in [
        "The parties agree to comply with all applicable data protection laws.",
        "The data protection officer contact details are as follows: privacy@example.com.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # Genuine data-protection obligations still fire.
    for sentence, risk in [
        ("Any data transferred outside the EEA requires appropriate safeguards.", "MEDIUM"),
        ("Under GDPR, subcontractor shall act as a data processor.", "LOW"),
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses and clauses[0].risk.value == risk, sentence


def test_warranty_and_standstill_period_not_a_term():
    # A warranty-coverage window ("warranty period is 12 months from
    # delivery") and a procedural standstill ("standstill period of 60 days")
    # are not the agreement's term.
    for sentence in [
        "The warranty period is 12 months from the date of delivery.",
        "The standstill period of 60 days shall not be shortened.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # Genuine agreement terms still fire.
    for sentence, risk in [
        ("The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024.", "MEDIUM"),
        ("The sponsorship period is 12 months from January 1, 2025.", "LOW"),
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses and clauses[0].risk.value == risk, sentence


def test_insurance_contributions_not_a_coverage_obligation():
    # "insurance contributions to the pension fund" are statutory payroll
    # contributions, not a negotiated insurance-coverage type.
    for sentence in [
        "The employer shall make insurance contributions to the pension fund.",
        "Insurance contributions are deducted from each employee's salary.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_contract_assignment_not_a_data_or_ip_transfer():
    # "assign / transfer this/the Agreement" is a contract-assignment (or
    # novation) right — it is not a personal-data transfer (both word orders
    # covered) nor an IP-rights transfer.
    for sentence in [
        "Either party may transfer this Agreement to an affiliate upon written notice.",
        "The Licensee shall not assign or transfer the Agreement to a third party without consent.",
    ]:
        for rule in ("data_protection", "ip"):
            clauses = [c for c in _clauses(sentence) if c.clause == rule]
            assert clauses == [], f"{rule}: {sentence}"
    # A genuine cross-border data transfer and a genuine IP assignment both fire.
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")


def test_negotiation_and_objection_windows_not_a_term():
    # A "negotiate in good faith for a period of N days" deadlock window and a
    # "period for objections of N days" are procedural response windows, not
    # the agreement's term.
    for sentence in [
        "The parties shall negotiate in good faith for a period of 30 days before mediation.",
        "The period for objections shall be 14 days from the date of such notice.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term still fires.
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_security_prices_not_a_contract_price():
    # A redemption/strike price of shares or options prices a security, not
    # the goods/services price — the price rule must not fire.
    for sentence in [
        "The redemption price of the shares shall be EUR 100 per share.",
        "The strike price of each Option shall be USD 10.00.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    # A genuine supply price still fires.
    clauses = [c for c in _clauses(
        ["The price of the first batch shall be EUR 5,000 per unit."]
    ) if c.clause == "price"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")
