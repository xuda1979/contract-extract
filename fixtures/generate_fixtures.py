#!/usr/bin/env python3
"""Deterministic fixture generation for the contract-extract test suite.

Every fixture is produced by this script so the repo stays reproducible:
  cd fixtures && python3 generate_fixtures.py

Generated outputs:
  fixtures/contracts/*.docx  (python-docx, created fresh each run)
  fixtures/contracts/*.pdf   (pypdf, written deterministically)

Because DOCX files are zip archives embedding *generation timestamps* in
their internal XML, running this script twice produces byte-different .docx.
That is expected and harmless: they are regenerated from the same source and
the *text contract* is identical.  PDFs are byte-identical across runs.
"""

from __future__ import annotations

import io
import os
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "contracts"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# fixture texts (10 PDF + 10 DOCX).  Each is a template with {meta} blocks.
# ---------------------------------------------------------------------------

_PDF_TEXTS = [
    {
        "file": "supply_agreement_clean.pdf",
        "format": "pdf",
        "title": "Supply Agreement",
        "sentences": [
            "This Supply Agreement is entered into between Acme Supply Co. and Beta Retail Ltd.",
            "The term of this Agreement shall be for a period of 2 years, effective from January 1, 2024.",
            "Buyer shall purchase a total of 10,000 units of the Products during each Contract Year at the price of USD 24.50 per unit.",
            "Seller shall deliver the Products within 45 working days from receipt of a purchase order.",
            "Acceptance shall occur within 15 days after delivery.",
            "Payment shall be net 30 days from the date of invoice.",
            "Either party may terminate this Agreement for convenience upon 90 days written notice.",
            "This Agreement shall automatically renew for successive 1 year terms.",
            "Each party retains all rights in its own intellectual property.",
            "Confidential information shall remain confidential for 3 years after termination.",
            "Neither party shall be liable for indirect, incidental or consequential damages.",
            "This Agreement shall be governed by the laws of Delaware.",
        ],
    },
    {
        "file": "software_license_risk.pdf",
        "format": "pdf",
        "title": "Software License Agreement (high risk)",
        "sentences": [
            "This Software License Agreement is made effective as of March 1, 2024.",
            "Licensee shall pay license fees of USD 500,000 payable in advance on execution.",
            "The initial term is 7 years from the effective date, and shall automatically renew for additional 5 year periods unless either party gives written notice.",
            "Liability of Licensor shall be limited to fees paid under this Agreement, not to exceed USD 500,000.",
            "The Licensor disclaims all warranties and shall not be liable for any damages whatsoever.",
            "In event of breach, Licensee shall pay Liquidated Damages of 30% of the total fees.",
            "Delivery of the licensed software shall occur within 90 days of the effective date.",
            "All works made for hire and related intellectual property shall vest in Licensor.",
            "Confidential obligations shall survive for 5 years following termination.",
            "Licensee shall not disclose confidential information to any third party.",
            "This Agreement includes an exclusive license grant to Licensee for a period of 6 years.",
            "The parties agree that all personal data shall be processed in accordance with GDPR.",
        ],
    },
    {
        "file": "master_service_agreement.pdf",
        "format": "pdf",
        "title": "Master Services Agreement",
        "sentences": [
            "This Master Services Agreement is entered into as of October 5, 2024.",
            "The initial term of this Agreement shall be 3 years, renewable.",
            "Company shall pay Provider monthly fees of USD 12,500.",
            "Provider shall deliver all Services within 20 business days of each Statement of Work.",
            "Client shall have 10 days to inspect and accept each deliverable.",
            "Payment terms are net 60 days after receipt of a valid invoice.",
            "In the event of breach, the defaulting party shall pay a penalty of 1.5% per month of the invoice amount.",
            "Each party's aggregate liability shall not exceed the amounts paid by Client in the preceding 12 months.",
            "This Agreement may be terminated by either party for convenience with 45 days written notice.",
            "The Parties acknowledge that confidential information includes pricing, customer lists and business plans.",
            "All intellectual property created by Provider shall be owned by Client upon full payment.",
            "Provider retains ownership of all pre-existing tools, frameworks and methodologies.",
            "Any data transferred outside the EEA requires appropriate safeguards.",
        ],
    },
    {
        "file": "procurement_bid_gov.pdf",
        "format": "pdf",
        "title": "Government Procurement Bid",
        "sentences": [
            "The Bidder shall deliver the Supplies within 120 calendar days after contract award.",
            "The delivery schedule is firm and penalties for late delivery apply at the rate of 0.5% per day of the contract value.",
            "The Contract Price is fixed at CNY 2,850,000.",
            "Payment shall be made in the following instalments: 30% on award, 60% on delivery, 10% on final acceptance.",
            "Final acceptance shall be completed within 60 days following delivery.",
            "The contract duration is 18 months.",
            "Confidentiality obligations apply to this bid process.",
            "Liability of the supplier shall be capped at the contract price.",
            "The bidder agrees that the employer may, at its option, extend the delivery period by up to 90 days.",
            "No termination for convenience is available to the employer before 12 months.",
        ],
    },
    {
        "file": "consulting_engagement_clean.pdf",
        "format": "pdf",
        "title": "Consulting Services Agreement",
        "sentences": [
            "This Engagements is for consulting services over a duration of 6 months.",
            "Consultant fees of USD 240,000 are payable monthly.",
            "The Consultant shall deliver all work within 5 business days of each milestone.",
            "Payment shall be net 30 days.",
            "Client may terminate for convenience with 30 days notice.",
            "The parties shall keep all business information confidential for 2 years.",
            "Independent Contractor status - no exclusivity is granted.",
            "Consultant retains ownership of all methods, process and materials it develops.",
            "Liability limited to fees actually paid.",
            "No automatic renewal - this Agreement expires at the end of term.",
        ],
    },
    {
        "file": "nondisclosure_bilateral.pdf",
        "format": "pdf",
        "title": "Mutual Non-Disclosure Agreement",
        "sentences": [
            "This Non-Disclosure Agreement is dated November 1, 2024.",
            "Confidential information means all non-public information disclosed by the parties.",
            "The obligations of confidentiality shall survive for a period of 5 years from disclosure.",
            "Neither party shall disclose confidential information to any third party without prior written consent.",
            "This NDA shall terminate upon completion of the purpose, or by either party upon 15 days written notice.",
            "Nothing in this Agreement grants any right to or license of any intellectual property.",
            "The parties shall only use confidential information for evaluation purposes.",
        ],
    },
    {
        "file": "distribution_partnership.pdf",
        "format": "pdf",
        "title": "Distribution & Partnership Agreement",
        "sentences": [
            "The term of this Agreement shall be 5 years from the effective date.",
            "Distributor is granted an exclusive right to sell the Products within the Territory for the duration of the Agreement.",
            "Distributor shall purchase a minimum of 1,000 units per year.",
            "Supply price is USD 12.00 per unit.",
            "Delivery shall be made within 60 days of order confirmation.",
            "Payment shall be net 45 days.",
            "In the event of late payment, interest of 2% per month shall accrue.",
            "Upon termination, the surviving provisions shall include confidentiality and intellectual property obligations.",
            "Distributor shall maintain the confidentiality of pricing and margins.",
            "Manufacturer's aggregate liability shall not exceed the amount of the purchase price paid by Distributor.",
        ],
    },
    {
        "file": "saas_terms_auto.pdf",
        "format": "pdf",
        "title": "SaaS Subscription Terms",
        "sentences": [
            "This SaaS Subscription renews automatically each month unless the customer cancels with notice of 30 days.",
            "Monthly subscription fee of USD 299 is charged to the customer's credit card on a recurring basis.",
            "The renewal term shall be 12 months.",
            "Confidentiality obligations shall survive termination for 3 years.",
            "Limitation of liability - each party's total liability is capped at the amount paid in the prior 12 months.",
            "The service shall be delivered with an uptime of 99.9%.",
            "Termination for cause requires the breach to remain uncured after 15 days written notice.",
            "Customer data shall be deleted within 90 days of termination.",
        ],
    },
    {
        "file": "subcontract_heavy.pdf",
        "format": "pdf",
        "title": "Heavy Construction Subcontract Agreement",
        "sentences": [
            "The initial term is 7 years ending December 31, 2030, unless the works complete earlier.",
            "Subcontractor quote: USD 4,520,000 total.",
            "Subcontractor shall provide all labour, materials and plant.",
            "Delivery of each work package shall be within 30 days of authorization.",
            "Retention of 10% of each invoice will be held until final acceptance.",
            "Final acceptance shall be 20 days after substantial completion.",
            "In default, subcontractor shall pay liquidated damages calculated at 1% per week of delay.",
            "Cap on liability shall be 100% of the subcontract price.",
            "All works made for hire vest in the contractor.",
            "Under GDPR, subcontractor shall act as a data processor.",
            "The subcontractor is granted an exclusive arrangement within this project.",
            "This Agreement may be terminated immediately by the contractor upon any default.",
        ],
    },
    {
        "file": "sponsorship_clean.pdf",
        "format": "pdf",
        "title": "Sponsorship Agreement",
        "sentences": [
            "The Sponsor agrees to pay the Organisation USD 50,000 in respect of the sponsorship.",
            "Payment shall be made in full within 30 days of signing.",
            "The sponsorship period is 12 months from January 1, 2025.",
            "Sponsor receives prominent logo placement and branding rights.",
            "This Agreement shall terminate on the expiry of the sponsorship period.",
            "No exclusivity is included in the standard sponsorship package.",
        ],
    },
]

_DOCX_TEXTS = [
    {
        "file": "employment_offer.docx",
        "format": "docx",
        "title": "Executive Employment Agreement",
        "paras": [
            "Employment Agreement",
            "This employment agreement is entered into as of July 1, 2024.",
            "The term of employment is 2 years.",
            "Base salary of USD 180,000 per annum, payable in equal monthly instalments.",
            "Bonus may be at the discretion of the board.",
            "The employee shall provide 3 months notice of resignation.",
            "Confidentiality obligations apply to all employer business information and survive termination for 3 years.",
            "All inventions created during employment are assigned to the employer.",
            "Upon termination for cause, the employer may recover any retention bonus paid.",
            "Either party may terminate this Agreement for cause.",
            "The executive shall not compete with the company for a period of 12 months after termination.",
            "Personal data of employees is processed under the employer's privacy policy.",
        ],
    },
    {
        "file": "loan_term_high.docx",
        "format": "docx",
        "title": "High-Interest Loan Term Sheet",
        "paras": [
            "Loan Agreement",
            "The Lender agrees to lend EUR 1,500,000 to the Borrower.",
            "The interest rate is 9.5% per annum.",
            "The loan term is 10 years.",
            "Late payment penalty of 5% per month of the overdue amount.",
            "The Borrower shall repay the principal in 120 monthly instalments.",
            "Default interest accrues at 2% above the base rate.",
            "Both parties agree disputes shall be settled by binding arbitration in Geneva.",
            "No confidentiality obligation applies to this facility.",
        ],
    },
    {
        "file": "joint_venture.docx",
        "format": "docx",
        "title": "Joint Venture Agreement",
        "paras": [
            "Joint Venture Agreement",
            "The Parties agree to establish a joint venture for 5 years.",
            "The capital contribution of Party A is USD 1,000,000; that of Party B is USD 500,000.",
            "Profits shall be shared equally between the parties.",
            "Either party may withdraw for convenience on 6 months written notice.",
            "Intellectual property of each party remains the property of that party.",
            "Neither party may assign this Agreement without the other's written consent.",
            "The joint venture may be terminated upon mutual agreement.",
            "Confidential information of each party shall be restricted to the venture purpose.",
        ],
    },
    {
        "file": "event_security.docx",
        "format": "docx",
        "title": "Event Security Services",
        "paras": [
            "Event Security Agreement",
            "Client engages Provider for event security services at a total of 12 events.",
            "Fees of USD 8,500 per event, total contract value USD 102,000.",
            "Provider shall deliver an on-site security plan at least 5 days before each event.",
            "Payment of each event invoice is net 30 days.",
            "During the event, Provider must complete acceptance inspection of the venue jointly with Client.",
            "Provider indemnifies Client against all claims arising from staff misconduct.",
            "Cap on Provider liability is limited to fees paid under this Agreement.",
            "Confidential handling of event logistics information is required.",
            "This Agreement is non-exclusive.",
        ],
    },
    {
        "file": "outsole_buyback.docx",
        "format": "docx",
        "title": "Repurchase / Buyback Agreement",
        "paras": [
            "Buyback Agreement",
            "Seller shall repurchase unsold inventory up to GBP 250,000.",
            "The repurchase price is fixed at 70% of the original purchase price.",
            "Buyback window ends 12 months from the effective date.",
            "Payment of the buyback amount shall be made within 90 days of the repurchase notice.",
            "Any products repurchased must be in resalable condition.",
            "Exclusivity: Seller appoints Buyer as the sole distributor of the products in the UK.",
            "This agreement lasts for the initial 3 years and renews automatically.",
        ],
    },
    {
        "file": "mfs_terms.docx",
        "format": "docx",
        "title": "MFS (Managed File Transfer) Terms",
        "paras": [
            "Terms of Service",
            "This is a Services Agreement for the provision of managed file transfer services.",
            "The monthly fee is USD 990 including support.",
            "All fees are payable in advance by credit card.",
            "Slotted delivery SLA: files delivered within 5 minutes for active transfers.",
            "If a data processing agreement is required, a DPA with standard contractual clauses applies.",
            "No liability for indirect losses; total liability limited to 3 months fees.",
            "Term is 24 months and auto-renews.",
        ],
    },
    {
        "file": "consulting_high_penalty.docx",
        "format": "docx",
        "title": "Consulting Agreement (High Penalty)",
        "paras": [
            "Consulting Agreement",
            "Consulting fees of USD 400,000 are payable over the term.",
            "Payment instalments: 50% on signing, 25% mid-project, 25% on completion.",
            "If the Consultant fails to meet a milestone, a penalty of 3% of the fees applies per week.",
            "All work product shall be assigned to the Client.",
            "The consultant agrees not to provide services to clients of the Company for 2 years after this agreement.",
            "Confidentiality: 5 years from the earlier of termination.",
            "The Company may not terminate for convenience within the first 8 months.",
        ],
    },
    {
        "file": "office_lease.docx",
        "format": "docx",
        "title": "Office Lease (Automatic Renewal)",
        "paras": [
            "Lease Agreement",
            "The initial term is 5 years commencing at opening of business.",
            "Base rent of USD 48,000 per year.",
            "Rent escalates 3% annually.",
            "Tenant may renew the lease for 2 further periods of 3 years each.",
            "If the tenant remains in possession after expiration, the lease shall automatically renew for 12 months.",
            "Security deposit of USD 12,000 required.",
            "Subletting is not permitted without the Landlord's consent, which may not be unreasonably withheld.",
            "Late rent penalty of 5% per month.",
        ],
    },
    {
        "file": "ad_agency_msa.docx",
        "format": "docx",
        "title": "Advertising Agency MSA",
        "paras": [
            "Master Services Agreement",
            "Agency will provide advertising and marketing services to the Client.",
            "The monthly retainer is USD 15,000.",
            "Media buying budget shall not exceed USD 2,000,000 in any calendar year.",
            "Client shall reimburse Agency for out-of-pocket costs within net 30 days.",
            "Acceptance of creative deliverables requires review within 10 business days of submission.",
            "Agency assigns to Client all intellectual property in the final deliverables upon receipt of full payment.",
            "The Agreement renews on a quarterly basis unless notice of non-renewal is given.",
            "Confidentiality includes campaign strategy and pricing.",
        ],
    },
    {
        "file": "early_termination_clean.docx",
        "format": "docx",
        "title": "Clean Service Agreement",
        "paras": [
            "Services Agreement",
            "The Services Agreement has a term of 12 months.",
            "Service fee of USD 36,000 per month.",
            "Either party may terminate this Agreement for convenience with 14 days written notice.",
            "The delivered services shall conform to the specification.",
            "Payment terms are net 30 days.",
            "Confidential information is protected for the term plus 1 year.",
            "There is no automatic renewal provision in this Agreement.",
            "The parties agree to negotiate in good faith on any extension.",
        ],
    },
]


# ---------------------------------------------------------------------------
# generation helpers
# ---------------------------------------------------------------------------


def _compose_text(spec: dict) -> str:
    """Build the full multi-paragraph text for DOCX fixtures."""
    if "paras" in spec:
        return "\n\n".join(spec["paras"])
    return "\n\n".join(spec["sentences"])


def _make_docx(spec: dict) -> None:
    import docx

    doc = docx.Document()
    doc.add_heading(spec["title"], level=0)
    doc.add_paragraph(f"Generated fixture: {spec['file']} . Do not treat as legal advice.")
    paras = _compose_text(spec).split("\n\n")
    for p in paras:
        doc.add_paragraph(p)
    doc.save(OUT / spec["file"])


def _make_pdf(spec: dict) -> None:
    import pypdf

    writer = pypdf.PdfWriter()
    # deterministic font settings so pages render consistently
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=A4)
    sheetsize = A4[1] - 100
    y = sheetsize
    for quote in spec["sentences"]:
        y -= 24
        if y < 60:
            c.showPage()
            y = sheetsize - 24
        c.drawString(60, y, quote)
    c.save()
    packet.seek(0)

    reader = pypdf.PdfReader(packet)
    for page in reader.pages:
        writer.add_page(page)

    with open(OUT / spec["file"], "wb") as fh:
        writer.write(fh)


def main() -> None:
    pdf_ok = docx_ok = 0
    for spec in _PDF_TEXTS:
        _make_pdf(spec)
        pdf_ok += 1
    for spec in _DOCX_TEXTS:
        _make_docx(spec)
        docx_ok += 1
    print(f"wrote {pdf_ok} PDF fixtures and {docx_ok} DOCX fixtures to {OUT}")


if __name__ == "__main__":
    main()
