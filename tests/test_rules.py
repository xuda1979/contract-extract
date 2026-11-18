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


def test_deed_signature_acceptance_not_fired():
    # "execute this Deed upon the acceptance of the signature page" is a
    # deed-execution / signature-formalities statement, not an acceptance
    # machinery clause.
    assert not _clauses(
        "The parties shall execute this Deed upon the acceptance of the appended signature page."
    )


def test_offer_acceptance_fires():
    # A genuine acceptance-machinery clause still fires: deemed acceptance
    # upon completion of the acceptance test.
    flags = _risk_by_rule(
        ["The Buyer shall be deemed to have accepted the Works upon completion of the acceptance test."],
        "acceptance",
    )
    assert flags


def test_insurer_claim_payment_not_a_payment_term():
    # An insurer's claims-settlement payment ("payment of the claim within 30
    # days of the loss assessment") is not a payment term between the
    # contracting parties.
    assert not _clauses(
        "The Insurer shall make payment of the claim within 30 days of the loss assessment."
    )


def test_payment_of_sum_due_fires():
    # "make payment of the purchase price within 30 days of the invoice" is a
    # genuine payment-term commitment between Buyer and Seller.
    flags = _risk_by_rule(
        ["The Buyer shall make payment of the purchase price within 30 days of the invoice date."],
        "payment",
    )
    assert flags


def test_copyright_duration_not_fired():
    # "duration of copyright protection shall be 70 years after the author's
    # death" is a statutory IP-protection term.
    assert not _clauses(
        "The duration of copyright protection shall be 70 years after the author's death."
    )


def test_salary_notice_penalty_not_fired():
    # "Failure to give the required notice shall attract a penalty of one
    # month's salary" is an employment remedy, not a penalty clause against a
    # contracting counterparty.
    assert not _clauses(
        "Failure to give the required notice shall attract a penalty of one month's salary."
    )


def test_statutory_product_liability_not_fired():
    # "The Supplier shall be liable to consumers for defective products under
    # the Product Liability Act" restates a statutory allocation.
    assert not _clauses(
        "The Supplier shall be liable to consumers for defective products under the Product Liability Act."
    )


def test_exclusive_dealing_not_fired():
    # "exclusive dealing that restricts competition" is a competition-law /
    # antitrust anti-arrangement statement.
    assert not _clauses(
        "The parties shall not engage in exclusive dealing that restricts competition."
    )


def test_tenant_renewal_option_fires():
    # A tenant's option to renew a lease remains a genuine renewal clause.
    flags = _risk_by_rule(
        ["The Tenant may renew the Lease for a further term by serving notice on the Landlord."],
        "renewal",
    )
    assert flags


def test_employee_termination_fires():
    # An employment termination right by notice is still a termination clause.
    flags = _risk_by_rule(
        ["The Employee may terminate this employment by giving notice to the Employer."],
        "termination",
    )
    assert flags


def test_bank_customer_confidentiality_fires():
    # A bank's duty to keep the affairs of its customer confidential is a
    # genuine confidentiality clause.
    flags = _risk_by_rule(
        ["The Bank shall keep the affairs of the Customer confidential in accordance with banking practice."],
        "confidentiality",
    )
    assert flags


def test_privacy_policy_publication_fires():
    # Publishing a privacy policy describing data processing is a genuine
    # data-protection clause.
    flags = _risk_by_rule(
        ["The Company shall publish a privacy policy describing how personal data is processed."],
        "data_protection",
    )
    assert flags


def test_adviser_recommended_insurance_not_fired():
    # "take out such insurance as its professional advisers recommend" defers
    # the coverage decision — not a concrete coverage obligation.
    assert not _clauses(
        "Each party shall take out such insurance as its professional advisers recommend."
    )


def test_attendees_own_insurance_not_fired():
    # Requiring attendees to hold their own insurance passes the burden to
    # individuals — not an inter-party coverage obligation.
    assert not _clauses(
        "The Organiser shall require attendees to hold their own insurance for the event."
    )


def test_quantity_of_data_not_fired():
    # "quantity of data to be transferred per month" is a technical
    # capacity/volume metric, not a personal-data movement obligation.
    assert not _clauses(
        "The contract shall specify the quantity of data to be transferred per month."
    )



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


def test_fixed_deposit_instrument_lifetime_not_a_duration():
    # "Fixed term deposits ... for a minimum period of 6 months" and "bank
    # guarantee ... in force for a period of 12 months" describe the lifetime
    # of a banking/financial product, not the agreement's term.  A genuine
    # agreement term still fires.
    for sentence in [
        "Fixed term deposits shall be offered for a minimum period of 6 months.",
        "The bank guarantee shall remain in force for a period of 12 months.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall remain in effect for a period of 3 years from the Effective Date."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_fiduciary_and_adverbial_exclusivity_not_a_market_grant():
    # "for the exclusive benefit of the beneficiaries" is a fiduciary/trust
    # obligation and "used exclusively for the evaluation" is an adverbial
    # use restriction — neither grants market exclusivity.  A genuine
    # "exclusive right to sell" still fires.
    for sentence in [
        "The trust estate shall be held for the exclusive benefit of the beneficiaries.",
        "The documents shall be used exclusively for the evaluation of the tender.",
        "The funds shall be applied exclusively for the purposes of the project.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_officers_dando_insurance_not_a_coverage_obligation():
    # D&O liability insurance procured for the company's own directors and
    # officers is an internal governance/benefit policy — not an inter-party
    # coverage obligation.  A genuine professional indemnity obligation still
    # fires.
    for sentence in [
        "Directors and Officers liability insurance shall be procured by the Company for its officers.",
        "The Corporation shall obtain D&O liability insurance for its management team.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance for the term of this Agreement."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_infringement_indemnity_not_an_ip_transfer():
    # "indemnify ... against third party infringement of intellectual
    # property" allocates risk for third-party IP claims — it does not
    # transfer, grant or own the parties' IP.  Genuine IP assignments still
    # fire.
    for sentence in [
        "The Supplier shall indemnify the Customer against third party infringement of intellectual property.",
        "The Contractor shall indemnify the Client against any infringement of third-party IP caused by the Works.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_contributory_negligence_not_a_cap():
    # Contributory-negligence / comparative-fault apportionment shares damages
    # between the parties under tort doctrine — it is not an agreed cap,
    # disclaimer or exclusion.  A genuine cap still fires.
    for sentence in [
        "Contributory negligence by the Buyer shall reduce the Seller's liability in proportion.",
        "Comparative fault shall reduce the liability of the party at fault.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The aggregate liability of each party shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_lex_loci_choice_of_law_not_a_cap():
    # "Liability ... determined in accordance with the law of the place of
    # the harmful event" is a conflicts-of-law rule naming the forum's law —
    # not an agreed cap, disclaimer or exclusion.  A genuine cap still fires.
    for sentence in [
        "Liability shall be determined in accordance with the law of the place of the harmful event.",
        "The liability of the parties shall be governed by lex loci delicti.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The aggregate liability of each party shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_demerit_points_penalty_not_a_contract_penalty():
    # "A penalty of 3 points shall be deducted from the licence" is an
    # administrative demerit-points sanction on a licence — not a contractual
    # default penalty for a breach.  A quantified late-payment penalty still
    # fires.
    for sentence in [
        "A penalty of 3 points shall be deducted from the licence for the traffic violation.",
        "The court imposed a penalty of 2 points on the driver's licence.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Contractor shall pay a penalty of USD 1,000 per day for each day of delay."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_bare_confidentiality_definition_not_an_obligation():
    # "Confidential Information means information that is not generally
    # known" is a definitional statement — it imposes no disclosure
    # restriction.  A genuine non-disclosure obligation still fires.
    for sentence in [
        "Confidential Information means information that is not generally known and has commercial value.",
        "Confidential Information does not include information that is publicly available.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Licensee shall not disclose confidential information to any third party."]
    ) if c.clause == "confidentiality"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_gdpr_purpose_limitation_not_a_data_clause():
    # "Personal data shall be collected for ... purposes only" restates the
    # GDPR purpose-limitation principle — it names no processing, transfer
    # or breach obligation.  A cross-border transfer still fires.
    for sentence in [
        "Personal data shall be collected for specified, explicit and legitimate purposes only.",
        "Personal data shall be processed only for the purposes stated in this Policy.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_approval_authority_not_market_exclusivity():
    # "exclusive authority to approve expenditures" is an internal governance
    # approval limit — not a grant of market exclusivity.  A genuine
    # exclusive right to sell still fires.
    for sentence in [
        "The Chief Executive Officer shall have the exclusive authority to approve expenditures above USD 50,000.",
        "The Board shall have the exclusive power to authorize any acquisition of the Company.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_retained_ownership_ip_not_assigned():
    # "intellectual property rights ... remain vested in the Licensor" is a
    # retention statement — ownership never moves, so it must not be read as
    # a one-way assignment to the other party.  Such statements may still
    # fire as a LOW informational ip clause, but never at HIGH assignment
    # risk.  A genuine assignment still fires at HIGH.
    for sentence in [
        "All intellectual property rights in the Standard Software shall remain vested in the Licensor.",
        "All intellectual property rights in the Works shall remain vested with the Contractor.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert all(c.risk.value != "HIGH" for c in clauses), sentence
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses and clauses[0].risk.value == "HIGH"


def test_insure_verb_is_a_coverage_obligation():
    # "shall insure the Goods against loss or damage during transit" is a
    # genuine coverage obligation even though it uses the verb "insure"
    # rather than the noun "insurance".  It must fire a LOW insurance
    # requirement.
    clauses = [c for c in _clauses(
        ["The Supplier shall insure the Goods against loss or damage during transit."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_application_acceptance_not_a_deliverables_acceptance():
    # "The acceptance of the application shall be notified to the applicant"
    # is administrative paperwork (licensing / registration), not an
    # acceptance of deliverables.  A genuine acceptance window still fires.
    for sentence in [
        "The acceptance of the application shall be notified to the applicant in writing.",
        "The application shall be accepted by the licensing authority within 30 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_marking_as_confidential_not_a_restriction():
    # "Documents shall be marked as confidential" is a marking duty, not a
    # confidentiality restriction on use or disclosure.  A genuine
    # non-disclosure obligation still fires.
    for sentence in [
        "Documents shall be marked as confidential by the Supplier.",
        "The Supplier shall stamp the label confidential on each page of the report.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Licensee shall not disclose confidential information to any third party."]
    ) if c.clause == "confidentiality"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_personal_data_definition_not_a_data_clause():
    # "Personal data means any information relating to a natural person" is a
    # GDPR definitional statement — it names no processing, transfer or
    # breach obligation.  A cross-border transfer still fires.
    for sentence in [
        "Personal data means any information relating to an identified or identifiable natural person.",
        "Personal data is defined as any information relating to a natural person.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_exclusive_control_over_marketing_not_market_exclusivity():
    # "exclusive control over the marketing of the Products" is an internal
    # responsibility allocation, not a market-exclusivity grant to sell,
    # license or distribute.  A genuine exclusive right to sell still fires.
    for sentence in [
        "The Buyer shall have exclusive control over the marketing of the Products in the Territory.",
        "The Buyer shall have control of the marketing of the Products exclusively within the region.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_cif_and_cost_allocation_not_insurance_obligation():
    # "on CIF terms, cost, insurance and freight" is an INCOTERMS shipping
    # term and "the cost of transportation and insurance shall be borne by
    # the Buyer" is a cost-allocation statement — neither creates a coverage
    # obligation.  A genuine maintain-insurance obligation still fires.
    for sentence in [
        "The Goods shall be supplied on CIF terms, being cost, insurance and freight to the port of destination.",
        "The cost of transportation and insurance shall be borne by the Buyer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain marine insurance for the Goods during transit at its own cost and freight."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_loan_prepayment_not_a_payment_term():
    # "The Borrower may prepay the Loan" is loan-facility mechanics, not a
    # payment term for goods or services.  A genuine advance payment still
    # fires.
    for sentence in [
        "The Borrower may prepay the Loan in whole or in part at any time.",
        "The Loan may be prepaid by the Borrower in whole or in part at any time.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "payment"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Buyer shall pay an advance payment of 30% of the contract price upon signature."]
    ) if c.clause == "payment"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_loan_prepayment_penalty_not_a_default_penalty():
    # "A prepayment penalty ... early settlement of the Loan" is a loan
    # yield-protection charge, not a default penalty for a contractual
    # breach.  A genuine late-payment penalty and a prepayment penalty
    # without loan context still fire.
    for sentence in [
        "A prepayment penalty of 1% shall apply to any early settlement of the Loan.",
        "The Loan may be repaid early, subject to a prepayment penalty of 1%.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 1.5% per month shall apply to any overdue invoice."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"
    clauses = [c for c in _clauses(
        ["A prepayment penalty of USD 5,000 applies if the Buyer cancels the order within 7 days of delivery."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_survival_boilerplate_not_termination_right():
    # "Upon termination of this Agreement, the provisions of Sections 4 and
    # 7 shall survive" lists what outlives the agreement — not a party's
    # right to terminate it.  Genuine termination rights still fire.
    for sentence in [
        "Upon termination of this Agreement, the provisions of Sections 4 and 7 shall survive in accordance with their terms.",
        "The provisions of Section 6 shall survive the termination of this Agreement.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement without cause upon 60 days' written notice."]
    ) if c.clause == "termination"]
    assert clauses and clauses[0].risk.value == "MEDIUM"
    clauses = [c for c in _clauses(
        ["The Buyer may terminate this Agreement for cause if the Seller commits a material breach."]
    ) if c.clause == "termination"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_probe_r22_clean_families_stay_silent():
    # Additional Round 22 probe candidates that already stayed silent and
    # must keep doing so: "renewable energy" (renew\\w* token is not a term
    # renewal), "quantity surveyor" (a profession), "remain the property of"
    # (retention without a transfer verb), "sole responsibility" (a duty
    # allocation), premises delivery (real-estate conveyance), early
    # withdrawal (banking product term) and a no-warranty disclaimer.
    expects = [
        ("renewal", "The Buyer shall use renewable energy sources for its operations."),
        ("quantity", "The Contractor shall appoint a quantity surveyor to measure the Works."),
        ("ip", "Pre-existing know-how and materials shall remain the property of the Supplier."),
        ("exclusivity", "The Supplier shall have the sole responsibility for quality control of the Products."),
        ("delivery", "Delivery of the Premises shall take place on the Completion Date, at which time title shall pass to the Buyer."),
        ("penalty", "The Bank may charge a penalty for early withdrawal of the deposit."),
        ("liability_cap", "The Supplier makes no warranty that the Services will be error-free or uninterrupted."),
    ]
    for rule, sentence in expects:
        clauses = [c for c in _clauses(sentence) if c.clause == rule]
        assert clauses == [], (rule, sentence)


def test_acceptance_certificate_issuance_not_acceptance_window():
    # "The Acceptance Certificate shall be issued upon completion of the
    # Works" is construction / works paperwork, not a deliverables acceptance
    # window.  A certificate deadline with a numeric window still fires.
    for sentence in [
        "The Acceptance Certificate shall be issued upon the completion of the Works.",
        "An acceptance certificate shall be delivered once the installation is complete.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The inspection and acceptance of the Deliverables shall occur within 10 days after delivery."]
    ) if c.clause == "acceptance"]
    assert clauses, "numeric acceptance window must keep firing"


def test_discretionary_advance_payment_not_a_payment_term():
    # "Payments may be made in advance at the Buyer's discretion" is an
    # optional early-payment allowance, not a primary payment obligation.
    # A committed advance payment still fires.
    for sentence in [
        "Payments may be made in advance of delivery at the Buyer's discretion.",
        "At the Buyer's discretion, advances on the fees may be paid early.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "payment"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Buyer shall pay an advance payment of 30% of the contract price upon signature."]
    ) if c.clause == "payment"]
    assert clauses, "committed advance payment must keep firing"


def test_employee_disciplinary_penalty_not_a_contract_penalty():
    # "An employee who violates the safety policy shall incur a penalty of
    # one day's unpaid leave" is an HR disciplinary measure, not a contractual
    # default penalty.  A genuine late-payment / liquidated-damages penalty
    # still fires.
    for sentence in [
        "An employee who violates the safety policy shall incur a penalty of one day's unpaid leave.",
        "Workers who breach the code of conduct shall be subject to a suspension as a penalty.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 1.5% per month shall apply to any overdue invoice."]
    ) if c.clause == "penalty"]
    assert clauses, "late-payment penalty must keep firing"


def test_charter_commitment_renewal_not_term_renewal():
    # "The Board shall renew its commitment to the Diversity Charter" is a
    # corporate pledge reaffirmation, not a renewal of the agreement's term.
    # A genuine term renewal still fires.
    for sentence in [
        "The Board shall renew its commitment to the Diversity Charter on an annual basis.",
        "The parties shall renew their pledge to the declaration of principles each year.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Agreement shall be renewed automatically for successive one-year terms."]
    ) if c.clause == "renewal"]
    assert clauses, "term auto-renewal must keep firing"


def test_policy_deductible_not_insurance_obligation():
    # "The insurance policy shall contain a deductible of USD 1,000 per claim"
    # is policy mechanics, not an inter-party coverage obligation.  A coverage
    # obligation still fires.
    for sentence in [
        "The insurance policy shall contain a deductible of USD 1,000 per claim.",
        "A deductible of EUR 500 shall apply under the insurance arrangements.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_non_compete_restrictions_sunset_not_termination_right():
    # "The non-compete restrictions shall terminate upon the expiry of the
    # Restricted Period" is a covenant sunset, not a party's right to
    # terminate the agreement.  A genuine termination right still fires.
    for sentence in [
        "The non-compete restrictions shall terminate upon the expiry of the Restricted Period.",
        "Upon the expiry of the Restricted Period, the non-compete restrictions shall terminate.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement without cause upon 60 days' written notice."]
    ) if c.clause == "termination"]
    assert clauses, "party termination right must keep firing"
    # Fixture line 200: "This Agreement shall terminate on the expiry of the
    # sponsorship period." — the Agreement itself is the subject, so this
    # genuine termination must keep firing.
    clauses = [c for c in _clauses(
        ["This Agreement shall terminate on the expiry of the sponsorship period."]
    ) if c.clause == "termination"]
    assert clauses, "agreement termination on expiry must keep firing"


def test_non_compete_sunset_not_exclusivity_grant():
    # "The non-compete restrictions shall terminate upon the expiry of the
    # Restricted Period" is a covenant sunset, not a market-exclusivity
    # grant.  A genuine market grant still fires.
    for sentence in [
        "The non-compete restrictions shall terminate upon the expiry of the Restricted Period.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "market exclusivity grant must keep firing"


def test_platform_exclusive_basis_not_market_exclusivity():
    # "The Platform shall operate on an exclusive basis within the Programme"
    # describes an operating mode, not a market-exclusivity grant.  Genuine
    # market grants still fire.
    for sentence in [
        "The Platform shall operate on an exclusive basis within the Programme for the Term.",
        "The programme shall be made available on an exclusive basis to Platform participants.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "market exclusivity grant must keep firing"


def test_no_rights_assigned_or_transferred_not_ip_or_data():
    # "No rights in the Mark shall be assigned or transferred to the Licensee"
    # is an IP negation (no rights pass) — not an IP assignment and not a
    # personal-data transfer.  Both IP and data_protection stay silent.
    sentence = "No rights in the Mark shall be assigned or transferred to the Licensee."
    for rule in ("ip", "data_protection"):
        clauses = [c for c in _clauses(sentence) if c.clause == rule]
        assert clauses == [], (rule, sentence)
    # Genuine positive controls still fire for both rules.
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses, "IP assignment must keep firing"
    clauses = [c for c in _clauses(
        ["The Controller shall transfer the personal data to the Processor under the DPA."]
    ) if c.clause == "data_protection"]
    assert clauses, "data transfer must keep firing"


def test_wire_transfer_not_data_transfer():
    # "The purchase price of the Shares shall be paid by wire transfer" names
    # a payment mechanism, not a personal-data transfer.
    sentence = "The purchase price of the Shares shall be paid by wire transfer."
    clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
    assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Controller shall transfer the personal data to the Processor under the DPA."]
    ) if c.clause == "data_protection"]
    assert clauses, "data transfer must keep firing"


def test_probe_r23_genuine_keeps_still_fire():
    # The two Round 23 candidates that are GENUINE clauses must keep firing:
    # the confidentiality care-standard duty and the insurance own-coverage
    # allocation.
    clauses = [c for c in _clauses(
        ["The Recipient shall protect the Confidential Information with the same degree of care as its own information of a similar nature."]
    ) if c.clause == "confidentiality"]
    assert clauses, "care-standard duty must keep firing"
    clauses = [c for c in _clauses(
        ["Each party shall maintain its own insurance coverage for its employees and property."]
    ) if c.clause == "insurance"]
    assert clauses, "own-coverage allocation must keep firing"


def test_bank_instrument_delivery_not_goods_delivery():
    # "The bank shall deliver the Letter of Credit to the Beneficiary"
    # is trade-finance instrument issuance (conveying a payment
    # instrument), not a goods/services delivery obligation.
    sentence = "The bank shall deliver the Letter of Credit to the Beneficiary within 5 business days."
    clauses = [c for c in _clauses(sentence) if c.clause == "delivery"]
    assert clauses == [], sentence
    # Genuine goods-delivery obligation still fires.
    clauses = [c for c in _clauses(
        ["Seller shall deliver the Products within 45 working days from receipt of a purchase order."]
    ) if c.clause == "delivery"]
    assert clauses, "goods-delivery obligation must keep firing"


def test_audit_right_inspect_not_acceptance_or_exclusivity():
    # "exclusive right to inspect the Books and Records" is an audit /
    # verification right, not an acceptance window nor a market grant.
    sentence = "The Buyer shall have the exclusive right to inspect the Books and Records of the Seller."
    for rule in ("acceptance", "exclusivity"):
        clauses = [c for c in _clauses(sentence) if c.clause == rule]
        assert clauses == [], (rule, sentence)
    # Genuine acceptance window and market-exclusivity grant still fire.
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "deliverables acceptance window must keep firing"
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "market-exclusivity grant must keep firing"


def test_acceptance_by_conduct_of_terms_not_deliverables():
    # Assent-by-conduct and deemed-receipt statements are contract
    # formation / notice mechanics, not deliverables acceptance windows.
    negatives = [
        "The Customer's continued use of the Services constitutes acceptance of the amended terms.",
        "Any notice given by email shall be deemed accepted upon receipt.",
        "The Seller warrants that the signatures on the Certificate are genuine and accepted by the authorities.",
    ]
    for sentence in negatives:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    # Genuine acceptance window still fires.
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "deliverables acceptance window must keep firing"


def test_claim_notification_not_insurance_obligation():
    # Loss-reporting mechanics to the insurer/broker are claim
    # administration, not a coverage obligation to maintain a policy.
    sentence = "The insurance broker shall be notified of any claim within 30 days of the loss."
    clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
    assert clauses == [], sentence
    # Genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_probe_r24_genuine_keeps_still_fire():
    # The Round 24 candidates that are GENUINE clauses must keep firing:
    # quantity commitment and advance-payment obligation.
    clauses = [c for c in _clauses(
        ["The Buyer shall order a minimum quantity of 100 units per month."]
    ) if c.clause == "quantity"]
    assert clauses, "minimum-quantity commitment must keep firing"
    clauses = [c for c in _clauses(
        ["The Buyer shall make an advance payment of 30% of the contract price upon signature."]
    ) if c.clause == "payment"]
    assert clauses, "advance-payment obligation must keep firing"


def test_until_such_time_as_termination_not_duration():
    # Reference to termination at an undefined future point ("until such time as it is
    # terminated") is not a fixed-duration undertaking.
    clauses = [c for c in _clauses(
        ["This Agreement shall remain in effect until such time as it is terminated in "
         "accordance with clause 10."]
    ) if c.clause == "duration"]
    assert clauses == [], "at-will-until-termination must not fire duration"


def test_law_mandated_termination_not_party_right():
    # Termination compelled by law/regulation is not a party termination right.
    clauses = [c for c in _clauses(
        ["This Agreement shall terminate if required by any applicable law or regulatory "
         "authority."]
    ) if c.clause == "termination"]
    assert clauses == [], "law-mandated termination must not fire termination"


def test_role_appointment_expiry_not_party_termination():
    # Expiry of an appointment/office coupled with automatic termination is an HR/governance
    # event, not a contractual termination right.
    clauses = [c for c in _clauses(
        ["The appointment of the Representative shall terminate automatically upon the "
         "expiration of the Distribution Agreement."]
    ) if c.clause == "termination"]
    assert clauses == [], "role appointment expiry must not fire termination"


def test_construed_away_liability_language_not_a_cap():
    # Language construing something as a limitation of liability (or agreeing not to rely on
    # a limitation) creates no cap.
    clauses = [c for c in _clauses(
        ["Nothing in this clause shall be construed as a limitation of liability of the "
         "parties."]
    ) if c.clause == "liability_cap"]
    assert clauses == [], "construed-away language must not fire liability_cap"
    clauses = [c for c in _clauses(
        ["The parties hereby agree not to rely on any limitation of liability in respect "
         "of fraud."]
    ) if c.clause == "liability_cap"]
    assert clauses == [], "waiver of cap must not fire liability_cap"


def test_referenced_insurance_requirements_not_obligation():
    # Pointers to where insurance requirements are stated create no coverage obligation.
    clauses = [c for c in _clauses(
        ["The insurance requirements of the Employer are set out in the Contract Data."]
    ) if c.clause == "insurance"]
    assert clauses == [], "requirements pointer must not fire insurance"


def test_to_the_extent_insurance_not_primary_duty():
    # Conditional, buyer-driven insurance fallback is not a primary coverage obligation.
    clauses = [c for c in _clauses(
        ["To the extent required by the Buyer, the Supplier shall take out insurance for "
         "the Goods."]
    ) if c.clause == "insurance"]
    assert clauses == [], "to-the-extent conditional must not fire insurance"


def test_probe_r25_genuine_keeps_still_fire():
    # Round 25 guards must not suppress genuine clauses:
    # party termination rights, primary insurance duties, and real liability caps.
    clauses = [c for c in _clauses([
        "Either party may terminate this Agreement without cause upon 60 days' written notice.",
        "The Buyer may terminate this Agreement for cause if the Seller commits a material breach.",
    ]) if c.clause == "termination"]
    assert clauses, "party termination rights must keep firing"
    clauses = [c for c in _clauses([
        "The Supplier shall maintain professional indemnity insurance during the Term.",
        "The Supplier shall take out insurance for the Goods during transit at its own cost.",
    ]) if c.clause == "insurance"]
    assert clauses, "primary insurance duties must keep firing"
    clauses = [c for c in _clauses([
        "Each party's aggregate liability shall not exceed the amounts paid by Client in "
        "the preceding 12 months.",
    ]) if c.clause == "liability_cap"]
    assert clauses, "genuine liability caps must keep firing"


def test_final_acceptance_term_end_not_acceptance_window():
    # A term ending at final acceptance of the last deliverable defines the agreement's
    # duration by a milestone; it creates no window in which deliverables are inspected.
    clauses = [c for c in _clauses(
        ["The Term shall run from the Effective Date until the Final Acceptance of the "
         "last Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses == [], "term-end milestone must not fire acceptance"
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each "
         "Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "genuine acceptance window must keep firing"


def test_exclusivity_breach_reference_not_grant():
    # A penalty triggered by breach of exclusivity obligations elsewhere points back at
    # another clause; it does not itself grant an exclusive right.
    clauses = [c for c in _clauses(
        ["Any breach of the exclusivity obligations under clause 7 shall attract a "
         "penalty of USD 10,000."]
    ) if c.clause == "exclusivity"]
    assert clauses == [], "exclusivity cross-reference must not fire exclusivity"
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "grant of exclusive rights must keep firing"


def test_evidence_of_insurance_not_obligation():
    # Proof/certificate obligations evidence coverage; they do not impose coverage itself.
    clauses = [c for c in _clauses(
        ["The Contractor shall provide evidence of insurance coverage upon request."]
    ) if c.clause == "insurance"]
    assert clauses == [], "evidence-of-coverage must not fire insurance"
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_reimbursable_cost_cap_not_price():
    # A cap on reimbursable out-of-pocket costs is a budget limit, not a price for goods.
    clauses = [c for c in _clauses(
        ["Reimbursable costs shall not exceed USD 50,000 in any calendar year."]
    ) if c.clause == "price"]
    assert clauses == [], "reimbursable-cost cap must not fire price"
    clauses = [c for c in _clauses(
        ["The Customer shall pay for the Goods at the price of USD 5,000 per unit."]
    ) if c.clause == "price"]
    assert clauses, "genuine unit price must keep firing"


def test_purchase_order_expiry_not_termination_right():
    # A purchase order expiring automatically on its stated date is a lifecycle event,
    # not a party's contractual termination right.
    clauses = [c for c in _clauses(
        ["Each Purchase Order shall terminate automatically upon its stated expiry date."]
    ) if c.clause == "termination"]
    assert clauses == [], "purchase-order automated expiry must not fire termination"
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement without cause upon 60 days' written "
         "notice."]
    ) if c.clause == "termination"]
    assert clauses, "party termination right must keep firing"


def test_defects_liability_period_not_cap():
    # The defects liability period is the post-acceptance warranty window in
    # construction contracts, not an agreed cap on liability.
    clauses = [c for c in _clauses(
        ["Acceptance of the Works shall be deemed given upon the expiry of the "
         "defects liability period."]
    ) if c.clause == "liability_cap"]
    assert clauses == [], "defects liability period must not fire liability cap"
    clauses = [c for c in _clauses(
        ["The total liability of either party under this Agreement shall not exceed "
         "the Contract Price."]
    ) if c.clause == "liability_cap"]
    assert clauses, "genuine total-liability cap must keep firing"


def test_at_the_risk_insurance_not_obligation():
    # "at the risk of" allocates who bears insurance cost in transit; it does
    # not oblige either party to maintain a policy.
    clauses = [c for c in _clauses(
        ["Insurance of the Goods shall be at the risk of the Buyer during transit."]
    ) if c.clause == "insurance"]
    assert clauses == [], "risk-allocation 'at the risk of' must not fire insurance"
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the "
         "Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_survival_not_termination_right():
    # Survival/savings boilerplate preserves rights that outlive the ending;
    # it is not a grant of a right to terminate.
    clauses = [c for c in _clauses(
        ["Termination of the Agreement shall not affect the accrued rights of the "
         "parties."]
    ) if c.clause == "termination"]
    assert clauses == [], "survival clause must not fire termination"
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement without cause upon 60 days' "
         "written notice."]
    ) if c.clause == "termination"]
    assert clauses, "party termination right must keep firing"


def test_acceleration_not_termination_right():
    # Acceleration makes the unpaid balance due on a termination event; that is
    # a payment consequence, not a right to terminate.
    clauses = [c for c in _clauses(
        ["In the event of termination, the unpaid balance shall become due and "
         "payable immediately."]
    ) if c.clause == "termination"]
    assert clauses == [], "acceleration clause must not fire termination"
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement without cause upon 60 days' "
         "written notice."]
    ) if c.clause == "termination"]
    assert clauses, "party termination right must keep firing"


def test_copyright_life_not_ip_clause():
    # A term-of-agreement that tracks the copyright life is a duration clause,
    # not an IP assignment, grant or ownership provision.
    clauses = [c for c in _clauses(
        ["This Agreement shall continue for the life of the copyright in the Works."]
    ) if c.clause == "ip"]
    assert clauses == [], "copyright-life term must not fire ip"
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses, "genuine IP assignment must keep firing"


def test_invention_disclosure_not_transfer():
    # A disclosure/reporting duty changes no ownership; only an assignment,
    # grant or transfer changes who owns the IP.
    clauses = [c for c in _clauses(
        ["The Employee shall promptly disclose any inventions to the Company."]
    ) if c.clause == "ip"]
    assert clauses == [], "invention disclosure duty must not fire ip"
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses, "genuine IP assignment must keep firing"


def test_discuss_renewal_not_term_renewal():
    # A commitment to discuss renewal before expiry is negotiation lead-time,
    # not an agreed renewal or extension of the term itself.
    clauses = [c for c in _clauses(
        ["The parties shall discuss renewal of this Agreement at least 3 months "
         "before expiry."]
    ) if c.clause == "renewal"]
    assert clauses == [], "discuss-renewal lead time must not fire renewal"
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and any("auto-renew" in (c.flag or "") for c in clauses), \
        "automatic term renewal must keep firing"


def test_late_payment_interest_not_payment_terms():
    # Late-payment interest is an economic consequence of delay, not a payment
    # terms clause (no due date, sequence, tranche or method is fixed).
    clauses = [c for c in _clauses(
        ["Late payment shall attract interest at 2% per month on any overdue "
         "amounts."]
    ) if c.clause == "payment"]
    assert clauses == [], "late-payment interest must not fire payment"
    clauses = [c for c in _clauses(
        ["Net 30 days from the invoice date, the Customer shall pay the full "
         "amount."]
    ) if c.clause == "payment"]
    assert clauses, "genuine net-30 payment terms must keep firing"


def test_acceptance_procedure_pointer_not_window():
    # A pointer to a test plan states how acceptance is performed; it imposes
    # no review window or inspection procedure of its own.
    clauses = [c for c in _clauses(
        ["Acceptance tests shall be performed in accordance with the test plan "
         "set out in Schedule 2."]
    ) if c.clause == "acceptance"]
    assert clauses == [], "procedure pointer must not fire acceptance"
    clauses = [c for c in _clauses(
        ["The Buyer shall be deemed to have accepted the Goods if it fails to "
         "notify defects within 30 days."]
    ) if c.clause == "acceptance"]
    assert clauses and any("window" in (c.flag or "") for c in clauses), \
        "genuine acceptance window must keep firing"


def test_statutory_carve_out_not_cap():
    # A carve-out that refuses to cap non-excludable heads of liability (death /
    # personal injury) is the opposite of an agreed cap or disclaimer.
    clauses = [c for c in _clauses(
        ["Nothing in this Agreement shall limit either party's liability for "
         "death or personal injury caused by its negligence."]
    ) if c.clause == "liability_cap"]
    assert clauses == [], "statutory carve-out must not fire liability cap"
    clauses = [c for c in _clauses(
        ["Each party's aggregate liability under this Agreement shall not "
         "exceed the Contract Price."]
    ) if c.clause == "liability_cap"]
    assert clauses, "genuine total-liability cap must keep firing"


def test_own_acts_allocation_not_cap():
    # Allocating responsibility for one's own acts and omissions restates who
    # answers for what; it is not an agreed cap, disclaimer or exclusion.
    clauses = [c for c in _clauses(
        ["Each party shall be liable for the acts and omissions of its own "
         "personnel."]
    ) if c.clause == "liability_cap"]
    assert clauses == [], "own-acts allocation must not fire liability cap"
    clauses = [c for c in _clauses(
        ["Each party's aggregate liability under this Agreement shall not "
         "exceed the Contract Price."]
    ) if c.clause == "liability_cap"]
    assert clauses, "genuine total-liability cap must keep firing"


def test_public_domain_carve_out_not_confidentiality():
    # A definitional exclusion ("shall not include the public domain") says what
    # the term covers, not who must keep what secret.
    clauses = [c for c in _clauses(
        ["Confidential Information shall not include information in the public "
         "domain through no fault of the receiving party."]
    ) if c.clause == "confidentiality"]
    assert clauses == [], "public-domain carve-out must not fire confidentiality"
    clauses = [c for c in _clauses(
        ["The Receiving Party shall keep the Confidential Information strictly "
         "confidential for 3 years."]
    ) if c.clause == "confidentiality"]
    assert clauses, "genuine confidentiality obligation must keep firing"


def test_ip_bare_definition_not_clause():
    # Defining what Intellectual Property Rights means enumerates the scope of
    # the term; it does not assign, grant or transfer anything.
    clauses = [c for c in _clauses(
        ["Intellectual Property Rights means all patents, copyrights, trade "
         "marks and other similar rights."]
    ) if c.clause == "ip"]
    assert clauses == [], "bare IP definition must not fire ip"
    clauses = [c for c in _clauses(
        ["The Customer grants the Supplier a royalty-free licence to use the "
         "Customer's Intellectual Property during the Term."]
    ) if c.clause == "ip"]
    assert clauses, "genuine licence grant must keep firing"


def test_intend_to_renew_not_term_renewal():
    # An intention to renew on terms still to be agreed is an aspiration, not
    # an agreed extension of the term.
    clauses = [c for c in _clauses(
        ["The Parties intend to renew this Agreement on terms to be agreed "
         "between them."]
    ) if c.clause == "renewal"]
    assert clauses == [], "intention to renew must not fire renewal"
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses and any("auto-renew" in (c.flag or "") for c in clauses), \
        "automatic term renewal must keep firing"


def test_unless_renewed_expiry_not_renewal():
    # "shall terminate ... unless renewed" is a conditional continuation
    # formula (an expiry default), not a renewal of the term itself.
    clauses = [c for c in _clauses(
        ["This Agreement shall terminate on the expiry of the fixed Term "
         "unless renewed by the parties."]
    ) if c.clause == "renewal"]
    assert clauses == [], "unless-renewed expiry must not fire renewal"
    clauses = [c for c in _clauses(
        ["The lease shall automatically renew for 12 months unless the tenant "
         "gives written notice."]
    ) if c.clause == "renewal"]
    assert clauses and any("auto-renew" in (c.flag or "") for c in clauses), \
        "auto-renewal with notice carve-out must keep firing"


def test_competition_law_qualifier_not_grant():
    # Acknowledging that competition law qualifies exclusivity is a legal
    # qualifier, not a grant of exclusive rights to either party.
    clauses = [c for c in _clauses(
        ["Any exclusivity granted under this Agreement is subject to applicable "
         "competition law."]
    ) if c.clause == "exclusivity"]
    assert clauses == [], "competition-law qualifier must not fire exclusivity"
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the "
         "Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "grant of exclusive rights must keep firing"


def test_controller_compliance_not_data_clause():
    # Allocating statutory responsibility for compliance is a role statement,
    # not a processing, transfer or breach obligation.
    clauses = [c for c in _clauses(
        ["The Controller shall remain responsible for compliance with "
         "applicable data protection legislation."]
    ) if c.clause == "data_protection"]
    assert clauses == [], "controller compliance allocation must not fire"
    clauses = [c for c in _clauses(
        ["To the extent the Processor processes personal data, the Data "
         "Processing Agreement shall apply."]
    ) if c.clause == "data_protection"]
    assert clauses, "genuine processing obligation must keep firing"


def test_subrogation_waiver_not_obligation():
    # A duty to obtain a subrogation waiver instructs how an existing policy is
    # handled; it does not oblige either party to take out or maintain cover.
    clauses = [c for c in _clauses(
        ["The Vendor shall cause its insurers to waive rights of subrogation "
         "against the Customer."]
    ) if c.clause == "insurance"]
    assert clauses == [], "subrogation waiver must not fire insurance"
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during "
         "the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_insurance_information_not_a_coverage_obligation():
    # Providing "insurance information" with an order is a paperwork /
    # information-supply step, not an inter-party obligation to take out or
    # maintain a policy.  A genuine coverage obligation still fires.
    for sentence in [
        "The Customer shall provide insurance information with each purchase order.",
        "The Supplier shall furnish insurance particulars upon request of the Buyer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during "
         "the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_ip_address_not_intellectual_property():
    # Internet-protocol addresses (routing / networking) are admitted by the
    # "assign" gate keyword but are not intellectual property.  A genuine
    # assignment still fires.
    for sentence in [
        "Each party shall use reasonable care in handling the IP addresses assigned to it.",
        "The Supplier shall configure the network addresses for the IP subnet within 30 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Employee shall assign all inventions created during the "
         "Assignment to the Company."]
    ) if c.clause == "ip"]
    assert clauses, "genuine IP assignment must keep firing"


def test_tariff_penalty_rates_not_a_contract_penalty():
    # Penalty rates in a tariff / price schedule are an administrative rate
    # book, not a negotiated default penalty for a contractual breach.  A
    # genuine per-breach penalty tied to a rate still fires.
    for sentence in [
        "The penalty rates set out in the tariff schedule apply to late filings.",
        "Late filing charges apply at the rates listed in the fee schedule for penalties.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Penalties for late delivery apply at the rate of 0.5% per day of "
         "the contract value."]
    ) if c.clause == "penalty"]
    assert clauses, "genuine delivery penalty must keep firing"


def test_licence_renewal_fee_not_a_term_renewal():
    # Re-issuing an administrative credential (licence / permit) on payment
    # of a renewal fee is a credentials matter, not a renewal of the
    # agreement's term.  A genuine term renewal still fires.
    for sentence in [
        "The Licence shall be renewed annually upon payment of the renewal fee.",
        "The parties shall renew their accreditation on a biennial basis.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses, "genuine auto-renewal must keep firing"


def test_nothing_limits_right_to_terminate_not_a_grant():
    # "Nothing in this clause shall limit the right ... to terminate" is
    # proviso-preservation boilerplate protecting a right granted elsewhere —
    # it grants no right itself.  A genuine for-cause termination right still
    # fires.
    for sentence in [
        "Nothing in this clause shall limit the right of either party to terminate for cause.",
        "Nothing in this Agreement shall be construed as limiting the right to terminate upon material breach.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for cause by written notice."]
    ) if c.clause == "termination"]
    assert clauses, "genuine termination for cause must keep firing"


def test_premises_inspection_not_acceptance_window():
    # A right to inspect manufacturing premises / facilities (site audit or
    # quality control) is not an acceptance window for deliverables.  A
    # numeric acceptance window still fires.
    for sentence in [
        "The Supplier shall permit the Customer to inspect the manufacturing premises upon reasonable notice.",
        "The Customer may visit the production facilities for inspection at any time.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "deliverables acceptance window must keep firing"


def test_bank_acceptance_letter_not_acceptance_window():
    # A bank's "acceptance letter" under a documentary credit is trade
    # finance (a payment instrument), not acceptance of deliverables.
    # A genuine acceptance letter under an acceptance procedure still fires.
    for sentence in [
        "The Bank's acceptance letter shall be presented together with the shipping documents.",
        "An acceptance letter from the issuing bank must accompany the bill of lading.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Customer shall issue an acceptance letter within 10 days of delivery of the Deliverables."]
    ) if c.clause == "acceptance"]
    assert clauses, "deliverables acceptance letter must keep firing"


def test_refuse_to_accept_delivery_not_acceptance_window():
    # A duty not to refuse acceptance of delivery is a delivery-acceptance
    # performance obligation, not a window in which deliverables are
    # inspected.  A numeric acceptance window still fires.
    for sentence in [
        "The Buyer shall not unreasonably refuse to accept delivery of the Goods.",
        "The Customer may not refuse delivery of conforming Goods.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Customer shall have 10 days after delivery to inspect and accept each Deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "deliverables acceptance window must keep firing"


def test_penalty_clause_reasonableness_recital_not_penalty():
    # A recital that a penalty clause is reasonable / proportionate / valid
    # comments on an existing clause; it imposes no penalty itself.
    # A quantified penalty still fires.
    for sentence in [
        "The Parties agree that the penalty clause in clause 14 is reasonable and proportionate.",
        "The penalty clause is fair and enforceable as liquidated damages.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["A late payment penalty of 2% per month shall apply to overdue amounts."]
    ) if c.clause == "penalty"]
    assert clauses, "quantified late-payment penalty must keep firing"


def test_trade_secrets_statute_not_confidentiality_obligation():
    # Complying with a trade-secrets statute (as a matter of law) is not an
    # inter-party confidentiality duty.  A genuine care-standard duty still
    # fires.
    for sentence in [
        "The parties shall maintain the confidentiality of trade secrets as required by the Trade Secrets Act.",
        "Trade secrets shall be kept confidential pursuant to applicable legislation.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Recipient shall keep the Confidential Information strictly confidential for 3 years."]
    ) if c.clause == "confidentiality"]
    assert clauses, "confidentiality duty must keep firing"


def test_ip_ownership_recital_not_assignment():
    # A recital acknowledging that IP ownership is set out in a schedule
    # states where the deal terms live; it assigns / grants nothing.
    # A genuine transfer still fires.
    for sentence in [
        "The Parties acknowledge the ownership of Intellectual Property is set out in the Background IP schedule.",
        "Ownership of the intellectual property shall be as described in the Annex.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["All intellectual property created under this Agreement shall be owned by the Customer."]
    ) if c.clause == "ip"]
    assert clauses, "IP ownership transfer must keep firing"


def test_renewal_notice_lead_time_not_a_renewal():
    # A notice-of-renewal lead time (how many days before expiry notice must
    # be given) is notice mechanics, not a term renewal.  Auto-renewal still
    # fires.
    for sentence in [
        "Each party shall give written notice of renewal not less than 30 days before expiry.",
        "Notice of renewal shall be submitted prior to the end of the Initial Term.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses, "genuine auto-renewal must keep firing"


def test_automatic_expiry_on_date_not_termination_right():
    # "shall terminate automatically on the Expiry Date" is the natural end
    # of the term (expiry by effluxion of time), not a party's termination
    # right.  A genuine termination right still fires.
    for sentence in [
        "The Agreement shall terminate automatically on the Expiry Date without any further act.",
        "On the Expiry Date this Agreement shall automatically terminate.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement forthwith if the other is subject to insolvency proceedings."]
    ) if c.clause == "termination"]
    assert clauses, "genuine insolvency termination must keep firing"


def test_exclusivity_territorial_carve_out_not_a_grant():
    # An exception stating exclusivity does not apply in certain territories
    # narrows a grant made elsewhere; it grants nothing itself.  A genuine
    # market grant still fires.
    for sentence in [
        "The exclusivity in clause 5 shall not apply to territories outside the EU.",
        "This exclusivity clause shall not apply in the region.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Seller shall have the exclusive right to sell the Products in the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "market exclusivity grant must keep firing"


def test_anonymised_data_not_personal_data():
    # Stating that anonymised data is not personal data defines the scope of
    # the data-protection regime; it effects no processing or transfer.
    # A genuine transfer still fires.
    for sentence in [
        "Anonymised data shall not be treated as personal data under this Agreement.",
        "Personal data does not include data that has been anonymised.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Controller shall transfer the personal data to the Processor under the DPA."]
    ) if c.clause == "data_protection"]
    assert clauses, "data transfer must keep firing"


def test_data_subject_rights_reference_not_data_obligation():
    # Pointing at a data subject's statutory rights under an article is a
    # reference, not a processing / transfer duty.  A genuine transfer still
    # fires.
    for sentence in [
        "The data subject's rights under Article 15 shall be respected.",
        "Rights of data subjects under the applicable chapter shall be preserved.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Controller shall transfer the personal data to the Processor under the DPA."]
    ) if c.clause == "data_protection"]
    assert clauses, "data transfer must keep firing"


def test_insurance_defined_term_pointer_not_coverage():
    # A pointer to the meaning of "Liability" in the Insurance Policies is a
    # defined-term cross-reference, not a coverage obligation.  A coverage
    # obligation still fires.
    for sentence in [
        "Liability shall have the meaning given to it in the Insurance Policies.",
        "The term Insurance shall be interpreted as defined in the insurance policies.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_insurance_cover_availability_not_coverage():
    # A statement that insurance cover is available (at competitive rates) is
    # market commentary, not a duty to procure cover.  A coverage obligation
    # still fires.
    for sentence in [
        "The parties confirm that insurance cover is available at competitive rates.",
        "Coverage insurance is available in the market at reasonable cost.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier shall maintain professional indemnity insurance during the Term."]
    ) if c.clause == "insurance"]
    assert clauses, "coverage obligation must keep firing"


def test_liability_information_accuracy_not_cap():
    # Holding a party responsible for the accuracy/completeness of the
    # information it provides is a representation, not an agreed cap,
    # disclaimer or exclusion.  A genuine cap still fires.
    for sentence in [
        "The Company shall be liable for the accuracy of the information contained in this prospectus.",
        "The Bidder shall be liable for the completeness of the data submitted.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Each party's aggregate liability shall not exceed the amounts paid by Client."]
    ) if c.clause == "liability_cap"]
    assert clauses, "aggregate cap must keep firing"


def test_parent_guarantee_obligations_not_liability_cap():
    # A parent guarantee ("liable for the obligations of its Subsidiary")
    # allocates who answers for another entity's debts, not an agreed cap or
    # exclusion.  A genuine disclaimer still fires.
    for sentence in [
        "The Parent Company shall be liable for the obligations of its Subsidiary.",
        "The Guarantor is liable for the debts of the Borrower.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Licensor disclaims all warranties and shall not be liable for any damages."]
    ) if c.clause == "liability_cap"]
    assert clauses, "broad disclaimer must keep firing"


def test_liability_proportional_allocation_not_cap():
    # Allocating liability in proportion to each party's share (contribution
    # / apportionment) is not an agreed cap, disclaimer or exclusion.  A
    # genuine cap still fires.
    for sentence in [
        "The parties accept liability under this clause in proportion to their respective shares.",
        "Each party is liable for its obligations in proportion to its participation.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Each party's aggregate liability shall not exceed the amounts paid by Client."]
    ) if c.clause == "liability_cap"]
    assert clauses, "aggregate cap must keep firing"


def test_liability_comparative_fault_reduction_not_cap():
    # A comparative-fault reduction ("liability shall reduce to the extent
    # the Buyer contributed to the loss") is a tort apportionment, not an
    # agreed cap or exclusion.  A genuine cap still fires.
    for sentence in [
        "The Seller's liability shall reduce proportionately to the extent the Buyer contributed to the loss.",
        "Liability for the loss shall be reduced by the extent of the Buyer's contributory fault.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Manufacturer's aggregate liability shall not exceed the purchase price paid by Distributor."]
    ) if c.clause == "liability_cap"]
    assert clauses, "aggregate cap must keep firing"


def test_liability_excluded_to_extent_permitted_not_cap():
    # "liability is excluded to the extent permitted by law" is a
    # legal-qualifier saving provision, not an agreed exclusion.  A genuine
    # disclaimer still fires.
    for sentence in [
        "Liability is excluded to the extent permitted by law.",
        "The exclusion of liability operates only to the extent allowed by applicable law.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Licensor disclaims all warranties and shall not be liable for any damages."]
    ) if c.clause == "liability_cap"]
    assert clauses, "broad disclaimer must keep firing"


def test_probation_period_not_duration():
    # An HR probation trial window is not the agreement's term.  A genuine
    # agreement term still fires.
    for sentence in [
        "The probation period of the employee shall be 3 months.",
        "The probationary term for new hires is 6 months.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 3 years from the effective date."]
    ) if c.clause == "duration"]
    assert clauses, "agreement term must keep firing"


def test_statute_of_limitations_not_duration():
    # The statutory window for bringing a claim is fixed by law, not the
    # agreement's term.  A genuine agreement term still fires.
    for sentence in [
        "The statute of limitations period is 2 years.",
        "The limitation period for warranty claims shall be 12 months.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 3 years from the effective date."]
    ) if c.clause == "duration"]
    assert clauses, "agreement term must keep firing"


def test_workmanship_repair_window_not_duration():
    # A post-completion snagging / repair window for defective workmanship is
    # not the agreement's term.  A genuine lease term still fires.
    for sentence in [
        "The Contractor is liable for the repair of defective workmanship for a period of 12 months after practical completion.",
        "Defects in the workmanship shall be remedied within a 6 month rectification period.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The term of the Lease shall be 5 years."]
    ) if c.clause == "duration"]
    assert clauses, "lease term must keep firing"


def test_prescription_renewal_not_contract_renewal():
    # Renewing a clinical prescription is medical, not a renewal of the
    # agreement's term.  A genuine auto-renewal still fires.
    for sentence in [
        "The patient must renew the prescription every 3 months.",
        "The vaccination shall be renewed each year.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This Agreement shall automatically renew for successive 1 year terms."]
    ) if c.clause == "renewal"]
    assert clauses, "auto-renewal must keep firing"


def test_court_awarded_payment_not_payment_terms():
    # A court judgment quantifying damages ("the Court awarded payment") is
    # not a payment term between the parties.  A genuine payment term still
    # fires.
    for sentence in [
        "The Court awarded payment of USD 50,000 in damages.",
        "The arbitral tribunal ordered payment of the claim within 30 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "payment"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Buyer shall pay the purchase price of USD 500,000 within 30 days of the invoice."]
    ) if c.clause == "payment"]
    assert clauses, "payment terms must keep firing"


def test_document_page_limit_not_quantity():
    # A page-length cap for a deliverable is not a quantity of goods.  A
    # genuine committed quantity still fires.
    for sentence in [
        "The report shall not exceed 5 pages.",
        "The submission shall not be more than 500 pages in length.",
        "No more than 20 pages of annexes may be included.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Buyer shall purchase a total of 10,000 units of the Products during each Contract Year."]
    ) if c.clause == "quantity"]
    assert clauses, "committed unit quantity must keep firing"


def test_player_transfer_window_not_data_transfer():
    # A sports player-transfer market window is not a personal-data transfer.
    # A genuine cross-border data transfer still fires.
    for sentence in [
        "The transfer window for players closes on 1 September.",
        "The trading window for registrations opens in January.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses, "cross-border transfer must keep firing"


def test_ip_clause_reference_not_ip_ownership():
    # A cross-reference to where the IP clause sits ("the IP clause in
    # section 9 allocates ownership") grants nothing itself.  A genuine
    # assignment still fires.
    for sentence in [
        "The intellectual property clause in section 9 allocates ownership of foreground IP.",
        "The IP provision in clause 7 deals with ownership of pre-existing tools.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Supplier grants the Customer an exclusive license to use the Software."]
    ) if c.clause == "ip"]
    assert clauses, "licence grant must keep firing"


def test_cancellation_fee_not_price():
    # An administrative cancellation charge for a booking/reservation is not
    # the price of goods or services.  A genuine price still fires.
    for sentence in [
        "A fee of USD 500 shall be charged for cancellation of the booking.",
        "The hotel shall charge a cancellation fee of EUR 100 for late cancellations.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The price of the Goods shall be USD 100 per unit delivered."]
    ) if c.clause == "price"]
    assert clauses, "goods price must keep firing"


def test_right_exclusive_to_holder_not_exclusivity():
    # "The right is exclusive to the holder" is a passive statement about who
    # holds a right, not a market-exclusivity grant.  A genuine exclusive
    # right still fires.
    for sentence in [
        "The right of first refusal is exclusive to the holder.",
        "The option is exclusive to the shareholder.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Distributor shall have the exclusive right to sell the Products within the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses, "exclusive right to sell must keep firing"


def test_settlement_terms_confidential_not_confidentiality_clause():
    # Keeping the terms/details of a settlement confidential is dispute-
    # resolution confidentiality, not the contract's own confidential
    # information.  A genuine confidentiality obligation still fires.
    for sentence in [
        "The parties shall keep the terms of this settlement confidential.",
        "The existence and details of the mediation shall remain confidential.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["All confidential information shall remain confidential for a period of 3 years after termination."]
    ) if c.clause == "confidentiality"]
    assert clauses, "confidentiality obligation must keep firing"


def test_knowledge_delivery_not_goods_delivery():
    # Presenting, training, or reporting to an audience is not the delivery
    # of goods or equipment.  A genuine goods-delivery deadline still fires.
    for sentence in [
        "We shall deliver a keynote presentation at the annual conference.",
        "The Consultant shall deliver training courses to staff.",
        "The consultant shall deliver the training programme within 30 days of commencement.",
        "The Committee shall deliver its report to the board within 30 days.",
        "The speaker shall deliver the lecture on the opening day.",
        "Deliverable: the contractor shall deliver the training materials.",
        "The team shall deliver the presentation slides to the client within 5 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "delivery"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Seller shall deliver the goods within 30 days of the purchase order."]
    ) if c.clause == "delivery"]
    assert clauses, "goods delivery deadline must keep firing"


def test_accepting_risk_not_goods_acceptance():
    # Accepting a risk, liability, or appointment is not accepting delivered
    # goods or works.  A genuine inspection-and-accept deadline still fires.
    for sentence in [
        "The Seller accepts payment via credit card.",
        "We accept payment by card or bank transfer.",
        "The tenant accepts the risk of theft.",
        "The Buyer accepts repossession of the vehicle.",
        "The Contractor accepts the appointment.",
        "The client accepts the tender submitted by the bidder.",
        "The Buyer accepts temporary repairs until final settlement.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Buyer shall have 10 days to inspect and accept each deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "inspection-and-accept deadline must keep firing"


def test_final_acceptance_milestone_not_acceptance_obligation():
    # A final-acceptance milestone after handover describes a stage in the
    # works process, not an acceptance obligation that binds a party.
    clauses = [c for c in _clauses(
        ["Final acceptance shall take place after site handover."]
    ) if c.clause == "acceptance"]
    assert clauses == []
    clauses = [c for c in _clauses(
        ["The Buyer shall have 10 days to inspect and accept each deliverable."]
    ) if c.clause == "acceptance"]
    assert clauses, "inspection-and-accept deadline must keep firing"


def test_non_contractual_penalties_not_penalty_terms():
    # Quiz points, participation fees, and financial-products early-exit
    # charges are not contractual penalty provisions.  A genuine delay
    # penalty still fires.
    for sentence in [
        "A penalty of 5 points applies for each incorrect answer in the quiz.",
        "Penalty shootout: the team prevailed 4-2.",
        "The team won the penalty shootout after extra time.",
        "An early withdrawal penalty of 3% applies to certificates of deposit.",
        "A penalty of 3% is charged on the redeemed units.",
        "The policy pays a penalty for late renewal of the insurance.",
        "The club shall pay a transfer fee of GBP 4m to the federation.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["The Contractor shall pay a penalty of 5% of the Contract Price for any delay."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_statutory_or_account_termination_not_contract_termination():
    # Reactions, accounts, and real-property instruments that expire by
    # operation of law are not contractual termination rights.  A genuine
    # termination provision still fires.
    for sentence in [
        "The reaction shall terminate when the temperature reaches 100 degrees.",
        "The subscription shall terminate upon account closure by the user.",
        "Your user account shall be terminated for violating the community guidelines.",
        "The grant shall terminate upon the death of the recipient.",
        "The easement shall terminate when the parcel is rezoned.",
        "The power of attorney shall terminate upon the donor's death.",
        "The offer shall terminate at 5pm unless accepted earlier.",
        "The appointment shall terminate at the end of the academic year.",
        "Tenure of the committee shall terminate upon dissolution.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for material breach."]
    ) if c.clause == "termination"]
    assert clauses, "termination provision must keep firing"


def test_accepting_payment_by_card_not_payment_terms():
    # Stating that a seller accepts card payments is a payment method, not a
    # payment obligation with terms.  A genuine invoicing or recurring-billing
    # term still fires.
    for sentence in [
        "The Seller accepts payment via credit card.",
        "We accept payment by card or bank transfer.",
        "The seller accepts payment through PayPal or by cheque.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "payment"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Invoices shall be paid within 30 days of receipt."]
    ) if c.clause == "payment"]
    assert clauses, "invoice payment term must keep firing"


def test_accepting_payment_by_card_not_data_transfer():
    # Paying by card is not a transfer of personal data.  A genuine
    # cross-border data transfer still fires.
    for sentence in [
        "The Seller accepts payment via credit card.",
        "We accept payment by card or bank transfer.",
        "The seller accepts payment through PayPal or by cheque.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA shall be subject to standard contractual clauses."]
    ) if c.clause == "data_protection"]
    assert clauses, "cross-border transfer must keep firing"


def test_consumer_insurance_arrangements_not_insurance_obligation():
    # Arranging or being encouraged to hold contents insurance is a personal
    # matter, not a party obligation to insure under the contract.  A genuine
    # professional-indemnity obligation still fires.
    for sentence in [
        "The tenant shall arrange home insurance for the contents.",
        "Each household is encouraged to hold contents insurance against fire.",
        "Flood insurance is recommended in high-risk zones.",
        "Each household is advised to hold possessions insurance.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_insurance_policy_metadata_not_insurance_obligation():
    # Policy metadata (insured value, named insured) describes a policy, it
    # does not obligate a party to insure.  A genuine insurance obligation
    # still fires.
    for sentence in [
        "The insured value of the vehicle is USD 20,000.",
        "The named insured is the policyholder listed in the schedule.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_insurance_market_movements_not_insurance_obligation():
    # Insurance products, premium movements, and member accident coverage are
    # market or product descriptions, not party obligations to insure.  A
    # genuine insurance obligation still fires.
    for sentence in [
        "The bank offers insurance products to its customers.",
        "The company sells insurance products across all channels.",
        "Insurance premiums for health plans rose 8% this year.",
        "Premium rates increased by 5% for motor policies.",
        "This policy covers personal accident insurance for members.",
        "Accident insurance for participants is included in the event plan.",
        "The policy pays a penalty for late renewal of the insurance.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["Provider shall carry professional indemnity insurance throughout the term."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_batch_or_lot_size_definition_not_a_quantity():
    # A batch/lot/sample "size" states a production or sampling specification
    # (how many units make up a batch), not a committed quantity of goods.
    for sentence in [
        "The batch size is 100 units.",
        "The lot size for each run shall be 50 units.",
        "A sample size of 400 units was inspected.",
        "The document is 30 pages long.",
        "The report shall be 12 pages long.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine committed quantity of goods still fires.
    clauses = [c for c in _clauses(
        ["Buyer shall purchase a total of 10,000 units under this Agreement."]
    ) if c.clause == "quantity"]
    assert clauses and clauses[0].risk.value == "LOW"


def test_market_or_securities_prices_not_a_contract_price():
    # Stock/share/market/retail/transfer prices price securities, markets or
    # intra-group transfers — not goods or services under this contract.
    for sentence in [
        "The stock price closed at USD 45 per share.",
        "The retail price of the product is USD 19.99.",
        "The share price shall not fall below EUR 10.",
        "A transfer price of USD 100 per MWh applies between the entities.",
        "The market price of the raw material reached CNY 5,000",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    # A genuine contract price still fires.
    clauses = [c for c in _clauses(
        ["The price of the first batch shall be EUR 5,000 per unit."]
    ) if c.clause == "price"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")


def test_price_transfer_pricing_not_a_data_protection_obligation():
    # Intra-group "transfer price" is an accounting rate for goods moved
    # between related entities, not a personal-data transfer.
    for sentence in [
        "A transfer price of USD 100 per MWh applies between the entities.",
        "The transfer price for the components shall be agreed annually.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine cross-border personal-data transfer still fires.
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_criminal_or_statutory_liability_not_a_liability_cap():
    # Criminal/tax/statutory liability names a legal regime under which a
    # person is prosecuted or assessed — not an agreed cap, disclaimer or
    # exclusion between the parties.
    for sentence in [
        "Criminal liability attaches to fraud under section 1.",
        "The employee remains liable for statutory liability under the Act.",
        "Tax liability on the benefit shall be borne by the employee.",
        "Liability for the offence is imposed on the operator.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine agreed liability cap still fires.
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_data_subject_liability_statement_not_a_clause():
    # "The data subject has no liability" allocates GDPR accountability — not
    # a processing/transfer obligation (data rule) nor an agreed cap
    # (liability rule).
    for sentence in [
        "The data subject has no liability for the controller's breach.",
        "Data subjects shall not bear any liability for processing carried out.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # Genuine obligations still fire.
    clauses = [c for c in _clauses(
        ["Any data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses


def test_periodical_subscription_renewal_not_a_term_renewal():
    # A magazine/newspaper periodical subscription renews a reader's
    # subscription, not the agreement's term.  The genuine SaaS-term renewal
    # anchor still fires.
    for sentence in [
        "The magazine subscription renews unless cancelled.",
        "The newspaper subscription shall renew automatically each year.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    clauses = [c for c in _clauses(
        ["This SaaS Subscription renews automatically each month unless the customer cancels with notice of 30 days."]
    ) if c.clause == "renewal"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_renewal_fee_not_a_contract_price():
    # A domain/membership renewal fee is an administrative billing amount
    # charged to re-issue an account, not the price of goods/services.
    for sentence in [
        "The domain name renewal fee is USD 12 per year.",
        "A renewal fee of USD 50 applies for the membership.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    # A genuine contract price still fires.
    clauses = [c for c in _clauses(
        ["Supply price is USD 12.00 per unit."]
    ) if c.clause == "price"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")


def test_sports_field_penalties_not_penalty_terms():
    # On-field sports sanctions ("yellow card", "penalty kick", "penalty
    # shot") are game events, not a contractual default penalty for a breach.
    for sentence in [
        "The player was shown a yellow card for the penalty.",
        "The penalty kick was converted by the striker.",
        "The referee awarded a penalty shot after the foul.",
        "Bobby earned a penalty in the football match.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine quantified breach penalty still fires.
    clauses = [c for c in _clauses(
        ["A late payment penalty of 2% per month applies to overdue amounts."]
    ) if c.clause == "penalty"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_parking_ticket_penalty_not_penalty_term():
    # A municipal parking/traffic ticket fine is a levy on a ticket, not a
    # negotiated contractual default penalty.
    for sentence in [
        "The penalty for late payment of the parking ticket is EUR 40.",
        "A parking fine penalty of EUR 80 applies to each ticket.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine penalty amount still fires.
    clauses = [c for c in _clauses(
        ["Penalty of USD 5,000 per day of delay after the notice period."]
    ) if c.clause == "penalty"]
    assert clauses


def test_copies_filed_with_institution_not_quantity():
    # Copies of a document filed/lodged with an institution (court, registry,
    # agency) or executed counterparts count duplicate originals, not a
    # quantity of goods.
    for sentence in [
        "The auditors filed 12 copies of the report with the court.",
        "The application was submitted in 8 copies to the registry.",
        "The deed was witnessed in 3 counterparts.",
        "Three counterparts of the agreement were executed.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine committed goods quantity still fires.
    clauses = [c for c in _clauses(
        ["The Buyer shall purchase 10,000 units of the Product."]
    ) if c.clause == "quantity"]
    assert clauses


def test_delivery_of_business_plan_or_proposal_not_goods():
    # Delivering a business plan / proposal to a corporate body is a document
    # submission, not a goods delivery obligation, even with a deadline.
    for sentence in [
        "The Contractor shall deliver the business plan to the Board within 14 days.",
        "The vendor shall deliver the proposal by 30 June.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "delivery"]
        assert clauses == [], sentence
    # A genuine goods delivery deadline still fires.
    clauses = [c for c in _clauses(
        ["The Seller shall deliver the goods to the buyer's warehouse within 30 days."]
    ) if c.clause == "delivery"]
    assert clauses


def test_transfer_of_lease_or_claim_not_data_transfer():
    # A transfer of a lease/tenancy/licence/claim requires consent or notice —
    # an assignment of a legal instrument, not a personal-data transfer.
    for sentence in [
        "The transfer of the lease requires the landlord's prior consent.",
        "The transfer of the claim shall be notified to the insurer.",
        "The transfer of the tenancy is subject to the lessor's approval.",
        "Any transfer of the licence requires written approval.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine cross-border data transfer still fires.
    clauses = [c for c in _clauses(
        ["Personal data transferred outside the EEA requires appropriate safeguards."]
    ) if c.clause == "data_protection"]
    assert clauses


def test_claim_assignment_notice_not_insurance_obligation():
    # Notifying a claim transfer to the insurer is claims-assignment
    # mechanics, not an inter-party coverage obligation.
    for sentence in [
        "The transfer of the claim shall be notified to the insurer.",
        "Assignment of the claim to the insurer shall be in writing.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["The Company shall maintain professional indemnity insurance."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value in ("LOW", "MEDIUM", "HIGH")


def test_media_exclusivity_not_exclusivity_grant():
    # Journalistic/media exclusivity (interview, footage, scoop) is content
    # given to one outlet — not a market-exclusivity grant to sell, license
    # or distribute.
    for sentence in [
        "The CEO gave an exclusive interview to the newspaper.",
        "The network aired the exclusive footage of the incident.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    # A genuine market exclusivity grant still fires.
    clauses = [c for c in _clauses(
        ["Distributor is granted an exclusive right to sell the Products within the Territory."]
    ) if c.clause == "exclusivity"]
    assert clauses


def test_trademark_or_patent_registration_verb_not_ip_term():
    # "trademark the brand / patent the device" are registration intentions,
    # not a transfer, grant or ownership clause about existing IP.
    for sentence in [
        "The Company shall trademark its new brand name before launch.",
        "The Company intends to patent the device in the US and EU.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # A genuine IP assignment still fires.
    clauses = [c for c in _clauses(
        ["The patent and trademark rights in the Works are assigned to the Customer."]
    ) if c.clause == "ip"]
    assert clauses


def test_interest_free_period_not_duration():
    # An interest-free period is a financing/billing window, not the
    # agreement's term.
    for sentence in [
        "The interest-free period is 30 days from the invoice date.",
        "An interest-free period of 60 days applies to the balance.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term still fires.
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_pet_owner_liability_not_liability_cap():
    # A pet-owner/animal tort duty is statutory, not an agreed cap or
    # disclaimer between the parties.
    for sentence in [
        "The owner shall be liable for damage caused by their pet.",
        "The keeper shall be liable for injuries caused by the animal.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine agreed liability cap still fires.
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_directors_duties_liability_not_liability_cap():
    # Directors/officers answering for their statutory duties is a corporate-
    # governance statement, not an agreed cap or disclaimer between the
    # parties.
    for sentence in [
        "Directors shall be liable for breach of their duties.",
        "The board members shall be liable for breach of their duties of care.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine agreed liability cap still fires.
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_employee_confidentiality_agreement_not_confidentiality_clause():
    # A mention of employee confidentiality agreements (HR paperwork) is not
    # an obligation between the parties over this contract's information.
    for sentence in [
        "Employee confidentiality agreements remain in force after termination.",
        "Staff confidentiality clauses bind all workers.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    # A genuine confidentiality obligation still fires.
    clauses = [c for c in _clauses(
        ["The parties shall keep all business information confidential for 2 years."]
    ) if c.clause == "confidentiality"]
    assert clauses


def test_returned_items_not_quantity():
    # Return batches, agenda entries and audit findings are not contractual
    # quantities of goods, units or copies.
    for sentence in [
        "The customer returned 3 items to the store.",
        "The report contains 12 items on the agenda.",
        "There were 5 defects found during the audit.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine procurement quantity still fires.
    clauses = [c for c in _clauses(
        ["The Customer shall deliver 500 items to the warehouse next month."]
    ) if c.clause == "quantity"]
    assert clauses


def test_quote_bid_not_price():
    # Price quotes, tenders and submitted bids are procurement estimates, not
    # the price agreed for goods or services under this contract.
    for sentence in [
        "The customer asked for a quote for the new widgets.",
        "We received a price quote of USD 500 for the repair.",
        "The bid price was USD 1,200,000 for the construction project.",
        "The tender price shall be submitted in sealed envelopes.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    # A genuine contracted price still fires.
    clauses = [c for c in _clauses(
        ["The price for the Goods shall be USD 500,000 payable within 30 days."]
    ) if c.clause == "price"]
    assert clauses


def test_notice_vesting_period_not_duration():
    # Notice timetables and equity vesting schedules are not the agreement's
    # term.
    for sentence in [
        "The notice period is 30 days, unless the parties agree otherwise.",
        "The vesting period for the options is 4 years.",
        "The retention period for personnel records is 5 years.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term still fires.
    clauses = [c for c in _clauses(
        ["The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024."]
    ) if c.clause == "duration"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_copyright_notice_registration_renewal_not_ip():
    # Marking duties, registry renewals and vesting schedules are not IP
    # ownership transfers.
    for sentence in [
        "The copyright notice appears on every page of the manual.",
        "The trademark registration was renewed for another 10 years.",
        "The vesting period for the options is 4 years.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # A genuine IP assignment still fires.
    clauses = [c for c in _clauses(
        ["The patent and trademark rights in the Works are assigned to the Customer."]
    ) if c.clause == "ip"]
    assert clauses


def test_employment_terminated_not_termination():
    # HR statements about the employment relationship being ended are not a
    # party's right to terminate the agreement.
    for sentence in [
        "The employment may be terminated by either party with 30 days' notice.",
        "The membership was terminated due to non-payment of fees.",
        "The subscription was cancelled by the user.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "termination"]
        assert clauses == [], sentence
    # A genuine termination right still fires.
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for convenience with 30 days' notice."]
    ) if c.clause == "termination"]
    assert clauses


def test_project_milestone_acceptance_not_acceptance_window():
    # Scheduling milestones (pilot runs, site acceptance dates) are not
    # inspection windows.
    for sentence in [
        "The acceptance test will take place after the pilot run.",
        "Site acceptance is scheduled for 31 December.",
        "The user accepted the invitation to the platform.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    # Genuine acceptance windows still fire.
    for sentence in [
        "Acceptance shall occur within 15 days after delivery.",
        "Final acceptance shall be completed within 60 days following delivery.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses, sentence


def test_privilege_and_settlement_fact_not_confidentiality():
    # Legal privilege, court-document release and the bare fact of settlement
    # are not inter-party confidentiality duties.
    for sentence in [
        "The opinion is privileged and confidential under attorney-client privilege.",
        "The confidential annex to the judgment was released to the press.",
        "The parties shall keep the fact of this settlement confidential.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    # A genuine confidentiality obligation still fires.
    clauses = [c for c in _clauses(
        ["The parties shall keep all business information confidential for 2 years."]
    ) if c.clause == "confidentiality"]
    assert clauses


def test_professional_statutory_liability_not_liability_cap():
    # Statutory/professional liability regimes and employee gross-negligence
    # duties are not agreed caps between the parties.
    for sentence in [
        "The auditor's professional liability shall not exceed the statutory cap.",
        "The statutory liability of the carrier is limited by the CMR Convention.",
        "The employee shall be liable for any loss caused by gross negligence.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine agreed liability cap still fires.
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_domain_account_transfer_not_data_protection():
    # Domain/account handovers are IT asset transfers, not personal-data
    # transfers.
    for sentence in [
        "The domain name shall be transferred to the buyer after payment.",
        "The account shall be transferred to the new administrator.",
        "The database shall be migrated to the new server next month.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine data transfer still fires.
    clauses = [c for c in _clauses(
        ["Personal data shall not be transferred outside the EEA."]
    ) if c.clause == "data_protection"]
    assert clauses


def test_consumer_and_statutory_insurance_not_insurance_requirement():
    # Consumer add-ons, event-risk arrangements and statutory/HR mandates are
    # not inter-party coverage obligations.
    for sentence in [
        "Cancellation insurance is offered to all travellers at check-out.",
        "The event was insured against adverse weather conditions.",
        "Health insurance is mandatory for all employees.",
        "The statutory insurance requirements are set out in the applicable law.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["The Seller shall maintain professional indemnity insurance."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_transitional_trial_period_not_duration():
    # HR/statutory staging periods are not the contract's own term.
    for sentence in [
        "The transitional period shall be 12 months from the effective date.",
        "The trial period of the software license is 30 days.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine contract term still fires.
    clauses = [c for c in _clauses(
        ["This Agreement shall remain in force for a period of 3 years."]
    ) if c.clause == "duration"]
    assert clauses


def test_batch_moq_not_quantity():
    # Production batch contents and minimum order requirements are not
    # deliverable quantity figures.
    for sentence in [
        "The batch contains 500 units of the vaccine.",
        "The minimum order quantity is 100 units.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine deliverable quantity still fires.
    clauses = [c for c in _clauses(
        ["The Seller shall deliver 1,000 units of the goods."]
    ) if c.clause == "quantity"]
    assert clauses


def test_list_price_not_contract_price():
    # Published list prices are catalogue values, not agreed purchase prices.
    clauses = [c for c in _clauses(
        "The list price of the item is USD 50."
    ) if c.clause == "price"]
    assert clauses == []
    clauses = [c for c in _clauses(
        ["The price of the goods shall be USD 50 per unit."]
    ) if c.clause == "price"]
    assert clauses


def test_clickwrap_and_rfp_acceptance_not_acceptance_window():
    # Consumer clickwrap consent and RFP award criteria are not contractual
    # acceptance/testing windows for delivered goods or works.
    for sentence in [
        "The end user must accept the terms before using the software.",
        "Terms of acceptance are set out in the request for proposal.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    # A genuine acceptance procedure still fires.
    clauses = [c for c in _clauses(
        ["The Buyer shall accept the Goods within 14 days of delivery."]
    ) if c.clause == "acceptance"]
    assert clauses


def test_misuse_and_state_war_liability_not_liability_cap():
    # End-user misuse duties and state war-exemption rules are not agreed
    # liability caps/exclusions between contracting parties.
    for sentence in [
        "The user assumes all liability for damage caused by misuse.",
        "The state shall not be liable for damages arising from war.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine exclusion/cap still fires.
    clauses = [c for c in _clauses(
        ["The aggregate liability of the Seller shall not exceed USD 1,000,000."]
    ) if c.clause == "liability_cap"]
    assert clauses


def test_medical_document_helpline_survey_not_confidentiality():
    # Privacy-flavoured statements about records, document markings,
    # helplines and survey data are not mutual confidentiality clauses.
    for sentence in [
        "The patient's medical records shall be kept confidential.",
        "This document contains confidential information.",
        "Customers may contact our confidential helpline at any time.",
        "The survey responses were collected confidentially.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "confidentiality"]
        assert clauses == [], sentence
    # A genuine confidentiality obligation still fires.
    clauses = [c for c in _clauses(
        ["The Receiving Party shall keep the Confidential Information secret."]
    ) if c.clause == "confidentiality"]
    assert clauses


def test_copyright_law_and_own_inventions_not_ip():
    # Statements about copyright protection and retention of one's own
    # inventions are not licence/grant clauses between the parties.
    for sentence in [
        "The logo is protected by copyright law.",
        "Employees retain intellectual property rights in their own inventions.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "ip"]
        assert clauses == [], sentence
    # A genuine assignment of inventions still fires.
    clauses = [c for c in _clauses(
        ["All inventions created during employment are assigned to the employer."]
    ) if c.clause == "ip"]
    assert clauses


def test_operation_of_law_termination_not_termination():
    # Statutory or automatic expiries are not agreed termination rights.
    clauses = [c for c in _clauses(
        "This Agreement shall terminate by operation of law."
    ) if c.clause == "termination"]
    assert clauses == []
    clauses = [c for c in _clauses(
        ["Either party may terminate this Agreement for convenience."]
    ) if c.clause == "termination"]
    assert clauses


def test_eez_hotel_exclusive_story_not_exclusivity():
    # Maritime zones, hospitality tiers and journalistic scoops are not
    # market-exclusivity grants.
    for sentence in [
        "The exclusive economic zone extends 200 nautical miles.",
        "The hotel offers exclusive packages for its guests.",
        "The newspaper published an exclusive story.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    # A genuine exclusive-right grant still fires.
    clauses = [c for c in _clauses(
        ["The Supplier shall have the exclusive right to sell the Products."]
    ) if c.clause == "exclusivity"]
    assert clauses


def test_gdpr_rights_pointer_not_data_protection_obligation():
    # A pointer to where data-subject rights live under GDPR is not a
    # processing, transfer or breach obligation.
    clauses = [c for c in _clauses(
        "The employee's data protection rights are set out in the GDPR."
    ) if c.clause == "data_protection"]
    assert clauses == []
    # Genuine GDPR obligations still fire.
    clauses = [c for c in _clauses(
        ["All personal data shall be processed in accordance with GDPR."]
    ) if c.clause == "data_protection"]
    assert clauses


def test_asset_holder_insurance_not_insurance_requirement():
    # Cover a holder places on its own assets (vehicles, equipment, buildings,
    # bundled travel benefits) is not an inter-party coverage obligation.
    for sentence in [
        "The car is insured for comprehensive coverage.",
        "The band insured their tour equipment against damage.",
        "Travel insurance is included in the ticket price.",
        "The company insured the building for USD 5 million.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        ["The Seller shall maintain professional indemnity insurance."]
    ) if c.clause == "insurance"]
    assert clauses and clauses[0].risk.value == "MEDIUM"


def test_compliance_and_securities_penalties_not_penalty_clause():
    # Statutory or market-conduct fines (compliance levies, insider-trading
    # sanctions, securities-regulator penalties) are not negotiated default
    # penalties for a contractual breach.
    for sentence in [
        "The plant faces a compliance penalty of USD 10,000.",
        "The new law doubles the penalty for insider trading.",
        "The company faces a civil penalty from the securities regulator.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine quantified breach penalty still fires.
    clauses = [c for c in _clauses(
        ["In the event of breach, the defaulting party shall pay a penalty of"
         " 1.5% per month of the invoice amount."]
    ) if c.clause == "penalty"]
    assert clauses


def test_call_protection_and_early_exit_not_penalty_clause():
    # Yield-protection / exit charges on financial instruments (bonds,
    # fixed-rate mortgages) are product terms, not default penalties for a
    # contractual breach.
    for sentence in [
        "The bond is subject to a call protection penalty of 1%.",
        "Lenders charge an early-exit penalty on fixed-rate mortgages.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "penalty"]
        assert clauses == [], sentence
    # A genuine late-delivery penalty still fires.
    clauses = [c for c in _clauses(
        ["The delivery schedule is firm and penalties for late delivery apply at"
         " the rate of 0.5% per day of the contract value."]
    ) if c.clause == "penalty"]
    assert clauses


def test_free_throw_penalty_not_penalty_clause():
    # On-field game-event sanctions (awarding a free throw after a foul) are
    # sports mechanics, not contractual default penalties.
    clauses = [c for c in _clauses(
        "The team was awarded a free throw after the penalty."
    ) if c.clause == "penalty"]
    assert clauses == []
    # A genuine penalty for milestone failure still fires.
    clauses = [c for c in _clauses(
        ["If the Consultant fails to meet a milestone, a penalty of 3% of the"
         " fees applies per week."]
    ) if c.clause == "penalty"]
    assert clauses


def test_vows_and_commitment_renewal_not_contract_renewal():
    # Idiomatic re-affirmations (vows, commitment, interest, efforts) are not
    # renewals of an agreement's term.
    for sentence in [
        "The partners renewed their vows after twenty years.",
        "The team renewed their commitment to the project.",
        "The success of the exhibition renewed interest in modern art.",
        "She renewed her efforts after the setback.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # A genuine auto-renewal term still fires.
    clauses = [c for c in _clauses(
        "This Agreement shall automatically renew for successive 1 year terms."
    ) if c.clause == "renewal"]
    assert clauses


def test_library_card_and_registration_renewal_not_contract_renewal():
    # Re-issuing a lending credential or an administrative registration is not
    # a renewal of the agreement's term.
    for sentence in [
        "The library card was renewed online for another year.",
        "The car registration was renewed for another twelve months.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # A genuine further-term renewal still fires.
    clauses = [c for c in _clauses(
        ["Tenant may renew the lease for 2 further periods of 3 years each."]
    ) if c.clause == "renewal"]
    assert clauses


def test_parliamentary_bill_renewal_not_contract_renewal():
    # Legislative procedure (re-introducing a bill for debate) is not a
    # renewal of the agreement's term.
    clauses = [c for c in _clauses(
        "The bill was renewed for debate in the next session of parliament."
    ) if c.clause == "renewal"]
    assert clauses == []
    # A genuine auto-renewal term still fires.
    clauses = [c for c in _clauses(
        "The initial term is 7 years from the effective date, and shall"
        " automatically renew for additional 5 year periods unless either"
        " party gives written notice."
    ) if c.clause == "renewal"]
    assert clauses


def test_certification_and_accreditation_renewal_not_contract_renewal():
    # Re-issuing a professional or institutional credential (a certification,
    # accreditation or qualification) is not a renewal of the agreement's term.
    for sentence in [
        "The engineer's certification must be renewed by 31 December.",
        "The company renewed its ISO certification last week.",
        "The school's accreditation must be renewed every five years.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence
    # A genuine renewal term still fires.
    clauses = [c for c in _clauses(
        "The renewal term shall be 12 months."
    ) if c.clause == "renewal"]
    assert clauses


def test_search_warrant_renewal_not_contract_renewal():
    # Re-issuing a judicial or investigatory instrument (a warrant or summons)
    # is not a renewal of the agreement's term.
    clauses = [c for c in _clauses(
        "The search warrant was renewed by the magistrate."
    ) if c.clause == "renewal"]
    assert clauses == []
    # A genuine term auto-renewal still fires.
    clauses = [c for c in _clauses(
        "Term is 24 months and auto-renews."
    ) if c.clause == "renewal"]
    assert clauses


def test_saas_subscription_renewal_is_contract_renewal():
    # A named SaaS subscription renewing automatically is still a genuine
    # term-renewal provision (distinct from magazine/domain renewals).
    clauses = [c for c in _clauses(
        "This SaaS Subscription renews automatically each year."
    ) if c.clause == "renewal"]
    assert clauses
    assert clauses[0].flag == "automatic renewal (auto-renew)"


# ---------------------------------------------------------------------------
# Round 39 — FP guard regression tests
# ---------------------------------------------------------------------------


def test_duration_prison_term_not_contract_term():
    # A criminal-sentence length (prison/jail/imprisonment term) is not the
    # agreement's term.
    for sentence in [
        "The court imposed a prison term of 5 years.",
        "The offender was given a jail term of 3 years.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term still fires.
    clauses = [c for c in _clauses(
        "The term of this Agreement shall be for a period of 2 years."
    ) if c.clause == "duration"]
    assert clauses


def test_quantity_units_of_vitamin_or_blood_not_quantity():
    # Nutritional/medical units ("units of vitamin D", "units of blood") are
    # not committed purchase quantities.
    for sentence in [
        "One serving of the cereal contains 200 units of vitamin D.",
        "The patient received 2 units of blood during the operation.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine purchase commitment still fires.
    clauses = [c for c in _clauses(
        "Buyer shall purchase a total of 10,000 units of the Products."
    ) if c.clause == "quantity"]
    assert clauses


def test_quantity_dwellings_inventory_capacity_not_quantity():
    # Counting dwellings, inventory stock, or throughput capacity is not a
    # committed purchase quantity.
    for sentence in [
        "The apartment block comprises 12 units, each with two bedrooms.",
        "The warehouse can store up to 5,000 units of inventory.",
        "The generator has a rated capacity of 1,000 units per hour.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "quantity"]
        assert clauses == [], sentence
    # A genuine per-year purchase commitment still fires.
    clauses = [c for c in _clauses(
        "Distributor shall purchase a minimum of 1,000 units per year."
    ) if c.clause == "quantity"]
    assert clauses


def test_acceptance_administrative_inspection_not_acceptance():
    # Administrative/statutory inspections (boiler, health, police, fire code,
    # technical, factory) are not acceptance of contract deliverables.
    for sentence in [
        "Annual inspection of the boiler is mandatory under the fire code.",
        "During the inspection, the auditor found no discrepancies.",
        "The inspection of the factory took place on 30 June.",
        "Registration requires a technical inspection every two years.",
        "The health inspector visited the restaurant for a routine inspection.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "acceptance"]
        assert clauses == [], sentence
    # A genuine deliverable acceptance inspection still fires.
    clauses = [c for c in _clauses(
        "During the event, Provider must complete acceptance inspection of"
        " the venue jointly with Client within 7 days."
    ) if c.clause == "acceptance"]
    assert clauses


def test_acceptance_of_resignation_not_acceptance():
    # Accepting a person's resignation is not acceptance of goods or services.
    clauses = [c for c in _clauses(
        "Acceptance of the resignation takes effect immediately."
    ) if c.clause == "acceptance"]
    assert clauses == []
    # A genuine acceptance obligation still fires.
    clauses = [c for c in _clauses(
        "The Buyer shall give its acceptance of the Goods in writing."
    ) if c.clause == "acceptance"]
    assert clauses


def test_liability_harassment_or_occupier_not_liability():
    # A general vicarious-employer exposure to staff harassment, or a
    # statutory occupier duty, is not an agreed liability cap.
    for sentence in [
        "The employer may be liable for harassment by its staff.",
        "Occupiers are liable to visitors under the Occupiers Liability Act.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine liability cap still fires.
    clauses = [c for c in _clauses(
        "The Supplier's aggregate liability shall not exceed the Purchase"
        " Price paid under this Agreement."
    ) if c.clause == "liability_cap"]
    assert clauses


def test_liability_insured_party_cover_not_liability():
    # "The company insured its directors" / "the bank insured the loan" are
    # cover arrangements taken by one party, not liability caps.
    for sentence in [
        "The company insured its directors against liability claims.",
        "The bank insured the loan against the default risk.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "liability_cap"]
        assert clauses == [], sentence
    # A genuine indemnity obligation still fires.
    clauses = [c for c in _clauses(
        "The Supplier shall indemnify and hold harmless the Buyer."
    ) if c.clause == "liability_cap"]
    assert clauses


def test_exclusivity_arrangement_ended_or_exclusive_access_not_exclusivity():
    # Reporting that a past exclusivity arrangement ended, or a membership
    # "exclusive access" perk, is not a market exclusivity grant.
    for sentence in [
        "The exclusivity arrangement came to an end last quarter.",
        "The club offers exclusive access to members only.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence
    # A genuine exclusive right still fires.
    clauses = [c for c in _clauses(
        "The Distributor is granted the exclusive right to sell the"
        " Products in the Territory."
    ) if c.clause == "exclusivity"]
    assert clauses


def test_data_protection_technical_or_credit_transfer_not_data():
    # IT-infrastructure data exchange and academic credit transfer involve no
    # personal-data processing or transfer obligation.
    for sentence in [
        "The system transfers data electronically to the central database.",
        "The parties shall exchange data in XML format between the servers.",
        "The applicant is entitled to 30 units of credit transfer.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "data_protection"]
        assert clauses == [], sentence
    # A genuine cross-border personal-data transfer still fires.
    clauses = [c for c in _clauses(
        "Personal data may be transferred outside the EEA only with"
        " appropriate safeguards."
    ) if c.clause == "data_protection"]
    assert clauses


def test_insurance_included_in_cost_not_insurance():
    # Cost-allocation statements ("cargo insurance included in the freight
    # cost") are not insurance-clause obligations.
    for sentence in [
        "Cargo insurance is included in the freight cost.",
        "Shipping insurance is included in the quoted price.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine professional-indemnity coverage obligation still fires.
    clauses = [c for c in _clauses(
        "Provider shall carry professional indemnity insurance throughout"
        " the term."
    ) if c.clause == "insurance"]
    assert clauses


def test_insurance_insured_event_or_sum_limit_not_insurance():
    # A claims-reporting instruction ("insured event ... reported") or a
    # policy sum-insured limit ("covers the insured up to EUR") is not an
    # inter-party coverage obligation.
    for sentence in [
        "The insured event must be reported within 30 days.",
        "The policy covers the insured up to EUR 1,000,000.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine insurance-requirement obligation still fires.
    clauses = [c for c in _clauses(
        "The Supplier shall maintain professional indemnity insurance"
        " with limits of at least USD 1,000,000."
    ) if c.clause == "insurance"]
    assert clauses


def test_insurance_directors_or_loan_cover_not_insurance():
    # Cover taken by one party on its own personnel/credit exposure is not an
    # inter-party insurance obligation.
    for sentence in [
        "The company insured its directors against liability claims.",
        "The bank insured the loan against the default risk.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "insurance"]
        assert clauses == [], sentence
    # A genuine coverage obligation still fires.
    clauses = [c for c in _clauses(
        "The Lessee shall insure the Goods against loss or damage during"
        " transit."
    ) if c.clause == "insurance"]
    assert clauses


def test_price_budget_allocation_not_price():
    # A budgetary allocation ("budget for the project is USD 100,000") is not
    # the price agreed for goods or services.
    for sentence in [
        "The budget for the project is USD 100,000.",
        "The advertised price of the car is USD 25,000.",
        "The fee for the membership is USD 30 per month.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "price"]
        assert clauses == [], sentence
    # A genuine budget cap (fixture-style "shall not exceed" price) still fires.
    clauses = [c for c in _clauses(
        "Media buying budget shall not exceed USD 2,000,000 in any calendar year."
    ) if c.clause == "price"]
    assert clauses


def test_duration_registration_or_certificate_term_not_duration():
    # Statutory / administrative registration and certificate lifetimes are
    # not the term of an agreement.
    for sentence in [
        "The term of the trademark registration is 10 years.",
        "The registration certificate is valid for 5 years.",
        "The building permit expires after 3 years.",
        "The warranty card is valid for 24 months.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "duration"]
        assert clauses == [], sentence
    # A genuine agreement term (fixture-style) still fires.
    clauses = [c for c in _clauses(
        "The term of this Agreement shall be for a period of 2 years,"
        " effective from January 1, 2024."
    ) if c.clause == "duration"]
    assert clauses


def test_acceptance_user_own_risk_not_acceptance():
    # A consumer assumption-of-risk disclaimer ("Users accept the app at their
    # own risk") is not an acceptance clause.
    clauses = [c for c in _clauses(
        "Users accept the app at their own risk."
    ) if c.clause == "acceptance"]
    assert clauses == []


def test_liability_latent_defects_by_statute_not_liability():
    # A statutory-liability pointer for latent defects is not a negotiated
    # liability allocation.
    clauses = [c for c in _clauses(
        "Liability for latent defects is imposed by statute."
    ) if c.clause == "liability_cap"]
    assert clauses == []


def test_renewal_subscription_discount_offer_not_renewal():
    # A consumer subscription-offer with a discount ("renew the subscription at
    # the discounted price") is not a renewal clause.
    clauses = [c for c in _clauses(
        "The customer may renew the subscription at the discounted price."
    ) if c.clause == "renewal"]
    assert clauses == []
    # A genuine renewal right (fixture-style, lease, no discount) still fires.
    clauses = [c for c in _clauses(
        "Tenant may renew the lease for a further 12 months."
    ) if c.clause == "renewal"]
    assert clauses


def test_renewal_reappointment_or_visa_extension_not_renewal():
    # A visa extension and a re-appointment are not contract-renewal clauses.
    for sentence in [
        "The visa was extended for another year.",
        "The contractor was re-appointed for a further term.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "renewal"]
        assert clauses == [], sentence


def test_confidentiality_minutes_not_confidentiality():
    # Meeting minutes that shall remain confidential are not a confidentiality
    # clause between the parties.
    clauses = [c for c in _clauses(
        "The minutes of the meeting shall remain confidential."
    ) if c.clause == "confidentiality"]
    assert clauses == []


def test_confidentiality_whistleblower_identity_not_confidentiality():
    # Keeping a whistleblower's identity confidential is a reporting-
    # protection statement, not a confidentiality clause.
    clauses = [c for c in _clauses(
        "The identity of the whistleblower was kept confidential."
    ) if c.clause == "confidentiality"]
    assert clauses == []


def test_confidentiality_trade_secrets_nature_not_confidentiality():
    # "Trade secrets are confidential by their nature" is a definitional
    # statement, not a confidentiality obligation.
    clauses = [c for c in _clauses(
        "Trade secrets are confidential by their nature."
    ) if c.clause == "confidentiality"]
    assert clauses == []


def test_ip_assigned_invention_not_ip():
    # A past-tense assignment statement ("was assigned") is a record of a
    # completed transfer, not an IP vesting clause.
    clauses = [c for c in _clauses(
        "The invention was assigned to the company by the inventor."
    ) if c.clause == "ip"]
    assert clauses == []
    # A genuine vesting obligation (fixture-style, present tense) still fires.
    clauses = [c for c in _clauses(
        "All inventions created during employment are assigned to the employer."
    ) if c.clause == "ip"]
    assert clauses


def test_ip_website_copyright_not_ip():
    # "The website is protected by copyright automatically" states the default
    # operation of copyright law, not an IP clause.
    clauses = [c for c in _clauses(
        "The website is protected by copyright automatically."
    ) if c.clause == "ip"]
    assert clauses == []


def test_ip_trademark_registration_not_ip():
    # A trademark-registration matter is an IP-office administration, not an
    # IP clause about the contract's work product.
    clauses = [c for c in _clauses(
        "The brand name is registered as a trademark."
    ) if c.clause == "ip"]
    assert clauses == []


def test_exclusivity_club_lounge_not_exclusivity():
    # A hotel's exclusive club lounge is a membership perk, not an exclusivity
    # clause.
    clauses = [c for c in _clauses(
        "The hotel's exclusive club lounge is for premium guests."
    ) if c.clause == "exclusivity"]
    assert clauses == []
    # A genuine exclusive-right grant still fires.
    clauses = [c for c in _clauses(
        "The exclusive licence covers the entire territory of France."
    ) if c.clause == "exclusivity"]
    assert clauses


def test_exclusivity_marketing_offers_not_exclusivity():
    # Marketing statements about exclusive offers are not exclusivity clauses.
    for sentence in [
        "Exclusive offers are emailed to registered customers.",
        "Exclusive offers are emailed to subscribers.",
    ]:
        clauses = [c for c in _clauses(sentence) if c.clause == "exclusivity"]
        assert clauses == [], sentence


def test_data_protection_data_subject_rights_not_obligation():
    # A GDPR individual-rights statement ("data subject has the right to
    # withdraw consent") is not a processing/transfer obligation.
    clauses = [c for c in _clauses(
        "The data subject has the right to withdraw consent at any time."
    ) if c.clause == "data_protection"]
    assert clauses == []
    # A genuine processing obligation (fixture-style) still fires.
    clauses = [c for c in _clauses(
        "All personal data shall be processed in accordance with GDPR."
    ) if c.clause == "data_protection"]
    assert clauses


def test_data_protection_dpo_appointment_not_obligation():
    # Appointing a data protection officer is organisational compliance, not a
    # processing/transfer obligation.
    clauses = [c for c in _clauses(
        "The controller shall appoint a data protection officer."
    ) if c.clause == "data_protection"]
    assert clauses == []


def test_insurance_premium_in_annual_fee_not_insurance():
    # A cost-allocation statement ("insurance premium included in the annual
    # fee") is not an insurance-clause obligation.
    clauses = [c for c in _clauses(
        "The insurance premium is included in the annual fee."
    ) if c.clause == "insurance"]
    assert clauses == []
    # A genuine premium-allocation statement ("payable by the tenant") still
    # fires.
    clauses = [c for c in _clauses(
        "Insurance is payable by the tenant under the lease."
    ) if c.clause == "insurance"]
    assert clauses


def test_insurance_coverage_commences_premium_not_insurance():
    # A policy effective-date rule ("coverage commences upon payment of the
    # first premium") is not an inter-party coverage obligation.
    clauses = [c for c in _clauses(
        "Insurance coverage commences upon payment of the first premium."
    ) if c.clause == "insurance"]
    assert clauses == []


def test_payment_payment_method_statement_not_payment_term():
    # A method-of-payment statement ("Payment may be made by bank transfer or
    # credit card" / "We accept most major credit cards for payment") is not a
    # payment term fixing an amount, due date or tranche.
    clauses = [c for c in _clauses(
        "Payment may be made by bank transfer or credit card."
    ) if c.clause == "payment"]
    assert clauses == []
    clauses = [c for c in _clauses(
        "We accept most major credit cards for payment."
    ) if c.clause == "payment"]
    assert clauses == []
    # A genuine payment term ("within 14 days of receipt") still fires.
    clauses = [c for c in _clauses(
        "The invoice must be paid within 14 days of receipt."
    ) if c.clause == "payment"]
    assert clauses


def test_payment_processing_or_registration_fee_or_stamp_duty_not_payment():
    # Payment-services tariffs, agency/statutory clearance fees and tax levies
    # named near "payable" are not inter-party payment terms.
    clauses = [c for c in _clauses(
        "The payment gateway charges a processing fee of 2%."
    ) if c.clause == "payment"]
    assert clauses == []
    clauses = [c for c in _clauses(
        "The registration fee payable to the agency is EUR 200."
    ) if c.clause == "payment"]
    assert clauses == []
    clauses = [c for c in _clauses(
        "Stamp duty on the deed is payable within 30 days."
    ) if c.clause == "payment"]
    assert clauses == []


def test_price_processing_restocking_handling_registration_fee_not_price():
    # Merchant / tariff / registration charges are not the price of goods or
    # services under this Agreement.
    for s in (
        "The payment gateway charges a processing fee of 2%.",
        "The supplier charges a restocking fee of 10% for returns.",
        "A handling fee of EUR 20 applies to each transaction.",
        "The registration fee payable to the agency is EUR 200.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "price"]
        assert clauses == []
    # A genuine quoted fee ("service fee of USD 500") still fires.
    clauses = [c for c in _clauses(
        "The Supplier shall pay a service fee of USD 500 upon delivery."
    ) if c.clause == "price"]
    assert clauses


def test_penalty_percent_of_turnover_not_penalty():
    # A statutory/regulatory revenue-share sanction ("a percentage of
    # turnover") is not a negotiated default penalty for a contractual breach.
    clauses = [c for c in _clauses(
        "The company faces a penalty of 4% of turnover for the breach."
    ) if c.clause == "penalty"]
    assert clauses == []
    # A genuine quantified late-payment penalty still fires.
    clauses = [c for c in _clauses(
        "A late payment penalty of 2% per month shall apply to overdue invoices."
    ) if c.clause == "penalty"]
    assert clauses


def test_confidentiality_status_statements_not_confidentiality():
    # Status / attribute statements (until publication, audience limitation,
    # treated-as-under-policy, descriptive discussions) are not agreed
    # secrecy duties between the parties.
    for s in (
        "The report remains confidential until publication.",
        "The negotiation positions are confidential to the two teams.",
        "Client information is treated as confidential under the policy.",
        "Confidential discussions took place at the summit.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "confidentiality"]
        assert clauses == []
    # A genuine secrecy obligation still fires.
    clauses = [c for c in _clauses(
        "Confidential information shall remain confidential for 3 years after termination."
    ) if c.clause == "confidentiality"]
    assert clauses


def test_insurance_holds_directors_and_officers_not_insurance():
    # A factual statement that the company currently holds a D&O policy is not
    # an inter-party coverage obligation.
    clauses = [c for c in _clauses(
        "The company holds directors and officers insurance."
    ) if c.clause == "insurance"]
    assert clauses == []
    # A genuine maintain/procure coverage obligation still fires.
    clauses = [c for c in _clauses(
        "The contractor shall maintain public liability insurance of USD 2m."
    ) if c.clause == "insurance"]
    assert clauses


def test_data_protection_payment_method_transfer_not_data():
    # A payment-method statement using the word "transfer" ("Payment may be
    # made by bank transfer") is not a personal-data movement.
    clauses = [c for c in _clauses(
        "Payment may be made by bank transfer or credit card."
    ) if c.clause == "data_protection"]
    assert clauses == []
    # A genuine personal-data transfer still fires.
    clauses = [c for c in _clauses(
        "Personal data may be transferred outside the EEA with consent."
    ) if c.clause == "data_protection"]
    assert clauses



def test_payment_statutory_fine_payable_not_payment():
    # A statutory levy/civil-penalty payment obligation ("fine payable within
    # 14 days") is not a contract price-payment duty between the parties.
    clauses = [c for c in _clauses(
        "The fine is payable within 14 days of the notice."
    ) if c.clause == "payment"]
    assert clauses == []
    # A genuine invoice payment duty still fires.
    clauses = [c for c in _clauses(
        "The invoice must be paid within 14 days of receipt."
    ) if c.clause == "payment"]
    assert clauses


def test_price_offered_at_price_or_government_price_not_price():
    # A quoted issuance/offer price ("offered ... at a price") or a state-set
    # utility tariff is not the price of goods or services under the
    # Agreement.
    for s in (
        "The shares were offered at a price of USD 10 per share.",
        "The government fixed the electricity price at EUR 0.30 per kWh.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "price"]
        assert clauses == []
    # A genuine contract price still fires.
    clauses = [c for c in _clauses(
        "The price of the first batch shall be EUR 5,000 per unit."
    ) if c.clause == "price"]
    assert clauses


def test_penalty_criminal_term_or_penalty_point_not_penalty():
    # A criminal-law sanction ("penalty of imprisonment") or a sports/contest
    # scoring reference ("penalty point") is not a contractual default
    # penalty.
    for s in (
        "The penalty for the offence is a term of imprisonment.",
        "The penalty point ended the game for the losing team.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "penalty"]
        assert clauses == []
    # A genuine quantified breach penalty still fires.
    clauses = [c for c in _clauses(
        "A late payment penalty of 2% per month shall apply to overdue invoices."
    ) if c.clause == "penalty"]
    assert clauses


def test_confidentiality_need_to_know_court_order_references_not_confidentiality():
    # A need-to-know standard, a court-ordered disclosure, or a confidential
    # references statement is not an agreed secrecy duty between the parties.
    for s in (
        "Disclosure is limited to staff on a need-to-know basis.",
        "The document was ordered confidential by the court.",
        "The candidate supplied two confidential references.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "confidentiality"]
        assert clauses == []
    # A genuine confidentiality obligation still fires.
    clauses = [c for c in _clauses(
        "Confidential information shall remain confidential for 3 years after termination."
    ) if c.clause == "confidentiality"]
    assert clauses


def test_exclusivity_dealership_franchise_route_not_exclusivity():
    # A dealership/franchise structure or an exclusive right over a transport
    # route is not an exclusive sales territory grant between the parties.
    for s in (
        "The parties agreed an exclusive dealership for the region.",
        "The brand is sold through an exclusive franchise network.",
        "The operator holds the exclusive right to the shipping route.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "exclusivity"]
        assert clauses == []
    # A genuine exclusive sales-rights grant still fires.
    clauses = [c for c in _clauses(
        "The distributor shall have the exclusive right to sell the products in Germany."
    ) if c.clause == "exclusivity"]
    assert clauses


def test_ip_exhaustion_goodwill_carveout_author_ownership_not_ip():
    # Exhaustion of rights, an IP exclusion from a sale, or a statutory
    # author-ownership statement is not a negotiated IP allocation under the
    # Agreement.
    for s in (
        "The resale of the goods exhausts the IP rights.",
        "Intellectual property is excluded from the sale of the business.",
        "The intellectual property in this report belongs to the author.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "ip"]
        assert clauses == []
    # A genuine assignment clause still fires.
    clauses = [c for c in _clauses(
        "All intellectual property rights created under this Agreement are assigned to the Customer."
    ) if c.clause == "ip"]
    assert clauses


def test_data_protection_sports_tax_physics_finance_transfer_not_data():
    # A sports transfer, a transfer tax, a physics/engineering transfer, or a
    # financial interest transfer is not a movement of personal data.
    for s in (
        "The goalkeeper's transfer was completed before the deadline.",
        "The land transfer tax is payable by the buyer.",
        "Heat transfer through the wall is minimized by insulation.",
        "The founder transferred an interest in the company to the investor.",
    ):
        clauses = [c for c in _clauses(s) if c.clause == "data_protection"]
        assert clauses == []
    # A genuine personal-data transfer still fires.
    clauses = [c for c in _clauses(
        "Personal data may be transferred outside the EEA with consent."
    ) if c.clause == "data_protection"]
    assert clauses


def test_acceptance_offer_lapses_formation_not_acceptance():
    # An offer-lapse formation rule ("offer lapses ... unless accepted") is a
    # contract-formation mechanic, not a goods acceptance procedure.
    clauses = [c for c in _clauses(
        "The offer lapses unless accepted within 14 days."
    ) if c.clause == "acceptance"]
    assert clauses == []
    # A genuine goods acceptance procedure still fires.
    clauses = [c for c in _clauses(
        "Acceptance of the goods requires inspection within 7 days."
    ) if c.clause == "acceptance"]
    assert clauses
