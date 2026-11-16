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


def test_product_coverage_renewal_not_a_term_renewal():
    # "renewal of the warranty", "support renewal for the software" and
    # "extended warranty renewal" extend product coverage after purchase —
    # they are not renewals of the agreement's term.
    for sentence in [
        "The supplier shall honour the renewal of the warranty for spare parts.",
        "Support renewal for the software is included in the annual fee.",
        "The extended warranty renewal must be purchased within 30 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # Genuine term renewals still fire.
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_integrity_reliance_and_sports_penalty_suppressed():
    # "rely exclusively on the statements in this Agreement" is an
    # entire-agreement / integrity clause and "granted exclusively to the
    # Licensee's employees" is an adverbial restriction — neither grants
    # market exclusivity.  A sports "penalty of 5 goals" is a game score,
    # not a contractual default penalty.
    for rule, sentence in [
        ("exclusivity", "The Buyer may rely exclusively on the statements in this Agreement."),
        ("exclusivity", "The parties placed exclusive reliance upon the representations made herein."),
        ("exclusivity", "Use of the Software is granted exclusively to the Licensee's employees."),
        ("penalty", "A penalty of 5 goals applies in the event of a tie."),
        ("penalty", "The losing team pays a penalty of 3 points in the league table."),
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == rule]
        assert clauses == [], f"{rule}: {sentence}"
    # Genuine market exclusivity and quantified contractual penalties still fire.
    clauses = [c for c in _clauses(
        ["Distributor is granted an exclusive right to sell the Products within the Territory for the duration of the Agreement."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value in ("MEDIUM", "HIGH")
    clauses = [c for c in _clauses(
        ["A penalty of EUR 500 per day applies to each late deliverable."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value in ("MEDIUM", "HIGH")


def test_market_benchmark_and_accounting_figures_not_contract_prices():
    # "average price of comparable products" benchmarks the market and
    # "notes payable" is a balance-sheet line item — neither is a price
    # under this contract, and "notes payable" is not a payment term either.
    for sentence in [
        "The average price of comparable products in the market is EUR 45.",
        "The balance sheet includes notes payable of USD 250,000.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause in ("price", "payment")]
        assert clauses == [], sentence
    # A genuine contract price and a loan principal still fire.
    clauses = [c for c in _clauses(
        ["The supply price is EUR 12.00 per unit."]
    ) if c.clause == "price"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")
    clauses = [c for c in _clauses(
        ["The Lender agrees to lend EUR 1,500,000 to the Borrower."]
    ) if c.clause == "price"]
    assert clauses


def test_money_rights_assignment_not_an_ip_transfer():
    # Assigning "rights to the payment of receivables" or "rights to receive
    # payments" moves money rights (factoring / receivables), not
    # intellectual property — the ip rule must not fire.
    for sentence in [
        "The Seller may assign its rights to the payment of receivables to a factoring company.",
        "Either party may assign its rights to receive payments to an affiliate without consent.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # Genuine IP transfers to the counterparty still fire.
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"
    clauses = [c for c in _clauses(
        ["All work product produced under this Agreement is the property of the Client."]
    ) if c.clause == "ip"]
    assert clauses


def test_supersede_clause_not_a_termination_right():
    # "supersedes and terminates any prior agreements" is supersession
    # boilerplate about earlier contracts — not a party's right to
    # terminate this Agreement.
    for sentence in [
        "This Agreement supersedes and terminates any prior agreements between the parties.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    # A genuine termination right still fires.
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for convenience upon 90 days written notice."]
    ) if c.clause == "termination"]
    assert clauses


def test_privacy_rights_carveout_not_a_data_clause():
    # "restricts the privacy rights of employees" is a fundamental-rights
    # carve-out, not a data-processing clause.  The employer's privacy
    # policy statement and a cross-border transfer still fire.
    for sentence in [
        "Nothing in this clause restricts the privacy rights of employees.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Personal data of employees is processed under the employer's privacy policy."]
    ) if c.clause == "data_protection"]
    assert clauses
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_underwriting_risk_acceptance_not_a_deliverables_clause():
    # "Acceptance of the risk under this policy" is an underwriting /
    # risk-appetite statement, not a deliverables acceptance.  Genuine
    # deliverable acceptance windows still fire.
    for sentence in [
        "Acceptance of the risk under this policy is governed by the insurer's underwriting guidelines.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Client shall have 10 days to inspect and accept each deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses


def test_quantity_discount_threshold_not_a_purchase_commitment():
    # "Quantity discounts of 5% for orders above 10,000 units" is a pricing
    # threshold, not a committed quantity.  A purchase commitment still fires.
    for sentence in [
        "Quantity discounts of 5% apply for orders above 10,000 units.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Buyer shall purchase 10,000 units in the first year."]
    ) if c.clause == "quantity"]
    assert clauses


def test_engagement_appointment_renewal_not_a_term():
    # "renew the engagement / appointment" renews a consulting relationship
    # or an office, not the agreement's term.  Genuine term renewals still
    # fire.
    for sentence in [
        "The parties may mutually agree to renew the engagement for a further term.",
        "The Board may renew the appointment of the auditor for another year.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses


def test_employment_termination_not_an_agreement_termination():
    # "Termination of the employment" is an HR event, not a party's right
    # to terminate the agreement.
    for sentence in [
        "Termination of the employment by the employee requires 30 days written notice.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for convenience upon 90 days written notice."]
    ) if c.clause == "termination"]
    assert clauses


def test_exclusive_rights_of_use_not_a_market_term():
    # "exclusive rights of use over the marketing materials" is a usage
    # right, not market exclusivity.  An exclusive right to sell still fires.
    for sentence in [
        "The parties grant each other exclusive rights of use over the joint marketing materials.",
        "The parties grant each other an exclusive right to use the joint marketing materials.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Distributor is granted an exclusive right to sell the Products within the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses


def test_commercial_privacy_of_not_a_data_clause():
    # "maintain the privacy of the negotiation strategy" protects a
    # commercial secret, not personal data.  Genuine data statements still
    # fire.
    for sentence in [
        "The Parties shall maintain the privacy of the negotiation strategy at all times.",
        "The Parties shall maintain the privacy of their deliberations.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_discretionary_cash_acceptance_not_a_deliverables_clause():
    # "Acceptance of cash payments is at the sole discretion of the seller"
    # is a payment-method statement, not a deliverables acceptance.
    for sentence in [
        "Acceptance of cash payments is at the sole discretion of the seller.",
        "Acceptance of payments shall be at the seller's sole discretion.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Client shall have 10 days to inspect and accept each deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_review_and_renew_planning_not_a_term_renewal():
    # "review and renew the annual budget / the forecasts shall be reviewed
    # and renewed" is an administrative planning cycle, not a renewal of the
    # agreement's term.
    for sentence in [
        "The parties shall review and renew the annual budget each October.",
        "The forecasts shall be reviewed and renewed on a quarterly basis.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_void_penalty_statement_not_a_penalty_clause():
    # "Any penalty clause ... shall be void and unenforceable" is a
    # legal-enforceability statement, not a default penalty.
    for sentence in [
        "Any penalty clause in this Agreement shall be void and unenforceable under applicable law.",
        "Penalties that are void under applicable law shall not be imposed.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Interest shall accrue on all late payments at 2% per month."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_benefits_package_insurance_not_a_coverage_obligation():
    # "medical insurance to employees as part of their benefits package" is
    # an employee-benefit perk, not an inter-party coverage obligation.
    for sentence in [
        "The Company shall provide medical insurance to employees as part of their benefits package.",
        "Health insurance is part of the employee benefits package.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance with limits of at least USD 1M."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_statutory_product_liability_not_a_cap():
    # "the parties acknowledge that product liability for defective goods
    # follows the manufacturer" restates a statutory allocation — no cap,
    # disclaimer or exclusion is agreed.
    for sentence in [
        "The parties acknowledge that product liability for defective goods follows the manufacturer.",
        "Product liability for defective products rests with the manufacturer under statute.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Manufacturer's aggregate liability shall not exceed USD 5,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_distributed_copies_not_a_committed_quantity():
    # "distributed in 5 copies to the committee members" counts copies of a
    # document distributed, not a committed quantity of goods.
    for sentence in [
        "The minutes of the meeting were distributed in 5 copies to the committee members.",
        "The report was distributed in 12 copies to the reviewers.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Buyer shall purchase 10,000 units in the first year."]
    ) if c.clause == "quantity"]
    assert clauses


def test_control_right_not_market_exclusivity():
    # "exclusive right to demand repayment / appoint and remove the auditor"
    # is a creditor or governance control right — not a market exclusivity
    # grant.  An exclusive right to sell still fires.
    for sentence in [
        "The Lender shall have the exclusive right to demand repayment of the Loan at any time.",
        "The Company shall have the exclusive right to appoint and remove the auditor.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Distributor is granted an exclusive right to sell the Products within the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses


def test_educational_course_period_not_agreement_duration():
    # "The course is offered for a period of 8 weeks each semester" is an
    # educational-program window, not the agreement's term.  A genuine term
    # ("This Agreement shall remain in force for a period of 3 years") still
    # fires.
    for sentence in [
        "The course is offered for a period of 8 weeks each semester.",
        "Each curriculum module runs for a term of 6 months.",
        "The degree programme lasts for a period of one academic year.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall remain in force for a period of 3 years."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_statutory_tax_penalty_not_contract_penalty():
    # "The tax penalty for late filing shall be 5% of the tax due" is a
    # statutory revenue sanction, not a negotiated default penalty.  A
    # genuine late-payment breach penalty still fires.
    for sentence in [
        "The tax penalty for late filing shall be 5% of the tax due.",
        "A VAT penalty applies for each month of delayed submission of returns.",
        "Interest shall accrue on the late filing penalty imposed by the revenue authority.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 2% per month shall apply to overdue invoices."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_statutory_tort_liability_not_agreed_cap():
    # "The Company's liability in tort shall be governed by the applicable
    # statute" allocates substantive law — no cap, disclaimer or exclusion
    # is agreed.  A genuine agreed cap still fires.
    for sentence in [
        "The Company's liability in tort shall be governed by the applicable statute.",
        "Liability for negligence shall be settled in accordance with the legislation.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier's aggregate liability shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_campaign_renewal_not_agreement_term_renewal():
    # "The parties may renew the campaign for the upcoming season" renews an
    # advertising/marketing campaign cycle, not the agreement's term.  A
    # genuine automatic term renewal still fires.
    for sentence in [
        "The parties may renew the campaign for the upcoming season.",
        "The marketing programme shall be renewed before each advertising season.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_employment_termination_verb_not_agreement_termination():
    # "The employee may terminate the employment contract by providing
    # notice" is an HR-event verb form — the employment is terminated, not
    # the agreement.  "Either party may terminate this Agreement" still
    # fires.
    for sentence in [
        "The employee may terminate the employment contract by providing 2 weeks notice.",
        "Terminate the employment contract at any time without penalty.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement upon 30 days written notice."]
    ) if c.clause == "termination"]
    assert clauses


def test_exclusive_basis_listing_not_market_exclusivity():
    # "The property is listed on an exclusive basis for 30 days" is a
    # real-estate listing instruction, not a market-exclusivity grant.  An
    # exclusive right to sell still fires.
    for sentence in [
        "The property is listed on an exclusive basis for 30 days.",
        "The premises are marketed through the broker on an exclusive basis.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Distributor is granted an exclusive right to sell the Products within the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses


def test_commodity_data_transfer_not_personal_data():
    # "The parties shall transfer the raw material data to the manufacturer"
    # moves commercial commodity data, not personal data.  A genuine
    # personal-data transfer still fires.
    for sentence in [
        "The parties shall transfer the raw material data to the manufacturer.",
        "Supplier shall furnish the product specification data to the buyer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any personal data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_certificate_issuance_not_insurance_obligation():
    # "The insurance certificate shall be issued within 5 business days" is
    # a documentary timeline, not a negotiated coverage obligation.  A
    # genuine coverage obligation naming a policy type still fires.
    for sentence in [
        "The insurance certificate shall be issued within 5 business days of signing.",
        "A cover note shall be delivered upon request of the insured.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Contractor shall maintain professional indemnity insurance with a limit of USD 1,000,000."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_perjury_declaration_not_a_contract_penalty():
    # "Under penalty of perjury" is the oath formula of a legal declaration,
    # not a contractual default penalty.  A quantified late-payment penalty
    # still fires.
    for sentence in [
        "Under penalty of perjury, the declarant certifies that the statements are true.",
        "The affidavit is made under penalty of perjury by the officer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 2% per month shall apply to any overdue amount."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_industrial_wage_premium_not_a_contract_penalty():
    # Night-shift / overtime "penalty rates" under an award are statutory
    # wage premiums, not negotiated default penalties for a breach.
    for sentence in [
        "Night-shift penalty rates of 150 percent shall apply under the award.",
        "Overtime shall be paid at a penalty rate of 200% of the base wage.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 2% per month shall apply to any overdue amount."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_several_not_joint_not_a_liability_cap():
    # "Liability ... several, not joint" allocates proportionate responsibility
    # among obligors — a doctrine of joint-and-several liability, not an
    # agreed cap or disclaimer.  A genuine aggregate cap still fires.
    for sentence in [
        "Each party's liability to the other shall be several, not joint.",
        "The liability of the joint venturers shall be several and not joint.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The aggregate liability of each party shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_grant_commitment_renewal_not_a_term_renewal():
    # "renew their grant commitments" renews a funding-programme commitment,
    # not the agreement's term.  A genuine automatic term renewal still fires.
    for sentence in [
        "The parties shall renew their grant commitments for the next cycle.",
        "The foundation renews its grant round on an annual basis.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_insurance_premium_adjustment_not_a_coverage_obligation():
    # "Insurance premium adjustments ... tariff bulletin" is administrative
    # bookkeeping on premium levels, not an inter-party coverage obligation.
    for sentence in [
        "Insurance premium adjustments shall be set out in the tariff bulletin.",
        "Premium schedules for the insurance shall be revised quarterly by the insurer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance for the term of this Agreement."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_cooling_off_period_not_a_duration():
    # "cooling-off period" is a statutory consumer-protection window (right
    # to cancel a distance purchase), not the agreement's term.  A genuine
    # agreement term ("period of 3 years") still fires.
    for sentence in [
        "The cooling-off period shall be 14 days from the date of the order.",
        "A cooling-off period of 7 days applies to the purchase.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall remain in effect for a period of 3 years from the Effective Date."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_project_termination_not_a_termination_clause():
    # "terminate the project / the engagement of the consultant shall
    # terminate" winds down a task or engagement, not a party's right to
    # terminate the agreement.  A genuine termination right still fires.
    for sentence in [
        "The Parties shall terminate the project upon completion of the Q3 deliverables.",
        "The engagement of the consultant shall terminate upon completion of the project.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement upon 30 days written notice."]
    ) if c.clause == "termination"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_sports_transfer_penalty_not_a_contract_penalty():
    # A football-club transfer sanction and a banking early-withdrawal fee
    # are stand-alone product/regime charges, not default penalties for a
    # contractual breach.  A quantified delay penalty still fires.
    for sentence in [
        "The Club shall pay a transfer penalty of EUR 5 million to the football association.",
        "The Club shall pay a transfer fee of EUR 5 million to the association.",
        "An early withdrawal penalty of 3% of the principal shall apply to deposits withdrawn before maturity.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Contractor shall pay a penalty of USD 1,000 per day for each day of delay."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_transfer_compound_not_a_data_transfer():
    # "transfer penalty / transfer fee" is a sports-federation compound, not
    # a personal-data transfer.  Genuine cross-border transfers still fire.
    for sentence in [
        "The Club shall pay a transfer penalty of EUR 5 million to the football association.",
        "No transfer payments shall be made to foreign clubs.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_rofr_on_shares_not_exclusive_market_right():
    # "exclusive right to purchase ... shares in a Change of Control" is a
    # right of first refusal over the company's own capital — a governance
    # right, not a market exclusivity grant.  Genuine market exclusivity
    # still fires.
    for sentence in [
        "The Company shall have the exclusive right to purchase all shares in the event of a Change of Control.",
        "The holder has the exclusive right to acquire the securities on a change of control.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_group_employee_insurance_not_a_coverage_obligation():
    # "Group life insurance shall be provided to all employees by the
    # employer" is an employer-funded employee-benefit perk, not an
    # inter-party coverage obligation.  A maintain/procure coverage
    # obligation still fires.
    for sentence in [
        "Group life insurance shall be provided to all employees by the employer.",
        "The employer shall provide health insurance to all staff members.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance for the term of this Agreement."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_non_infringement_warranty_not_ip_transfer():
    # A non-infringement warranty confirms the deliverables are free of
    # third-party IP — it is not a transfer, grant or ownership clause.
    # Genuine IP assignments still fire.
    for sentence in [
        "The Contractor warrants that it does not infringe third party intellectual property rights.",
        "The Supplier represents that no third-party rights are infringed by the Goods.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_strict_liability_doctrine_not_a_cap():
    # "creates strict liability" restates the tort doctrine — not an agreed
    # cap, disclaimer or exclusion.  Genuine caps and disclaimers still fire.
    for sentence in [
        "Nothing in this clause creates strict liability for latent defects in the Goods.",
        "Absolute liability shall arise upon delivery of the dangerous substance.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall not be liable for indirect or consequential damages."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_prohibited_by_law_carveout_not_a_cap():
    # "shall not apply to the extent prohibited by applicable law" is a
    # statutory carve-out limiting an indemnity's scope — not an agreed cap.
    # A genuine aggregate cap still fires.
    for sentence in [
        "The indemnity shall not apply to the extent prohibited by applicable law.",
        "This clause shall be void to the extent prohibited by applicable law.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The aggregate liability of each party shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"
