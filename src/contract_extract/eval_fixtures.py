"""Offline evaluation of the rule layer against an expert baseline.

Used by the CLI (`--fixtures`) and by the pytest suite.  The EXPECTED table
encodes, per fixture file, which clause fields must be present and at what
risk level.  It is derived from the structured texts in
fixtures/generate_fixtures.py — the same texts the 20 fixtures are generated
from, so it is a true ground truth rather than a self-fulfilling encoding of
the rule output.

Metrics (SPEC acceptance criteria):
  AC1  per-field recall   : expected field found >= 1 clause  (>= 95%)
  AC2  risk-flag accuracy : max extracted risk == expected risk per found
                            expected field                     (>= 90%)
  AC4  every field's clause always carries clause/page/quote/risk (schema enforced)
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # src/

from contract_extract.pipeline import extract_document
from contract_extract.rules.engine import document_risk
from contract_extract.schema import RiskLevel

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "contracts"

_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}

# filename -> {clause field: expected risk level}
EXPECTED: dict[str, dict[str, RiskLevel]] = {
    "supply_agreement_clean.pdf": {
        "duration": RiskLevel.MEDIUM,     # term 2 years
        "quantity": RiskLevel.LOW,        # 10,000 units
        "price": RiskLevel.LOW,           # USD 24.50 / unit
        "delivery": RiskLevel.LOW,        # within 45 days
        "acceptance": RiskLevel.MEDIUM,   # acceptance within 15 days
        "payment": RiskLevel.LOW,         # net 30 days
        "termination": RiskLevel.LOW,     # either party, for convenience
        "renewal": RiskLevel.MEDIUM,      # auto-renew 1 year
        "ip": RiskLevel.LOW,              # each party retains ownership
        "confidentiality": RiskLevel.LOW,
        "liability_cap": RiskLevel.LOW,   # exclusion of consequential damages
    },
    "software_license_risk.pdf": {
        "price": RiskLevel.MEDIUM,        # USD 500,000 payable in advance
        "duration": RiskLevel.HIGH,       # initial term 7 years
        "liability_cap": RiskLevel.HIGH,  # broad disclaimer of all warranties
        "penalty": RiskLevel.HIGH,        # liquidated damages 30%
        "delivery": RiskLevel.MEDIUM,     # within 90 days (long)
        "ip": RiskLevel.HIGH,             # work-for-hire vests one-way
        "confidentiality": RiskLevel.MEDIUM,  # survives termination
        "exclusivity": RiskLevel.HIGH,    # exclusive license 6 years
        "data_protection": RiskLevel.LOW, # GDPR compliance mentioned
        "renewal": RiskLevel.MEDIUM,      # auto-renew
    },
    "master_service_agreement.pdf": {
        "duration": RiskLevel.MEDIUM,     # initial term 3 years
        "price": RiskLevel.LOW,           # monthly fees USD 12,500
        "delivery": RiskLevel.LOW,        # within 20 business days
        "acceptance": RiskLevel.MEDIUM,   # 10 days to inspect and accept
        "payment": RiskLevel.MEDIUM,      # net 60 days
        "penalty": RiskLevel.MEDIUM,      # 1.5% per month
        "liability_cap": RiskLevel.MEDIUM,  # capped at amounts paid
        "termination": RiskLevel.LOW,     # either party, convenience
        "confidentiality": RiskLevel.LOW,
        "ip": RiskLevel.MEDIUM,           # assigned upon full payment
        "data_protection": RiskLevel.MEDIUM,  # cross-border transfer
    },
    "procurement_bid_gov.pdf": {
        "delivery": RiskLevel.MEDIUM,     # 120 days / up to 90 days
        "penalty": RiskLevel.MEDIUM,      # 0.5% per day
        "price": RiskLevel.LOW,           # CNY 2,850,000
        "payment": RiskLevel.MEDIUM,      # 30% tranche on award
        "acceptance": RiskLevel.MEDIUM,   # within 60 days
        "duration": RiskLevel.LOW,        # 18 months
        "confidentiality": RiskLevel.LOW,
        "liability_cap": RiskLevel.MEDIUM,  # capped at contract price
    },
    "consulting_engagement_clean.pdf": {
        "duration": RiskLevel.LOW,        # 6 months
        "price": RiskLevel.LOW,           # USD 240,000
        "delivery": RiskLevel.MEDIUM,     # within 5 days (aggressive)
        "payment": RiskLevel.LOW,         # net 30 days
        "termination": RiskLevel.MEDIUM,  # one-sided convenience
        "confidentiality": RiskLevel.LOW,
        "ip": RiskLevel.LOW,              # consultant retains ownership
        "liability_cap": RiskLevel.MEDIUM,
    },
    "nondisclosure_bilateral.pdf": {
        "confidentiality": RiskLevel.LOW,
        "termination": RiskLevel.LOW,
    },
    "distribution_partnership.pdf": {
        "duration": RiskLevel.MEDIUM,     # term 5 years
        "exclusivity": RiskLevel.MEDIUM,  # exclusive right to sell
        "quantity": RiskLevel.LOW,        # 1,000 units
        "price": RiskLevel.LOW,           # USD 12.00 / unit
        "delivery": RiskLevel.LOW,        # within 60 days
        "payment": RiskLevel.LOW,         # net 45 days
        "penalty": RiskLevel.MEDIUM,      # late-payment interest 2%/month
        "confidentiality": RiskLevel.LOW,
        "liability_cap": RiskLevel.MEDIUM,
    },
    "saas_terms_auto.pdf": {
        "renewal": RiskLevel.MEDIUM,      # auto-renew monthly, 30-day notice
        "price": RiskLevel.LOW,           # USD 299
        "payment": RiskLevel.MEDIUM,      # recurring credit-card billing
        "confidentiality": RiskLevel.MEDIUM,  # survives termination
        "liability_cap": RiskLevel.MEDIUM,
        "termination": RiskLevel.LOW,     # for cause
        "data_protection": RiskLevel.LOW,
    },
    "subcontract_heavy.pdf": {
        "duration": RiskLevel.HIGH,       # initial term 7 years
        "price": RiskLevel.LOW,           # USD 4,520,000
        "delivery": RiskLevel.LOW,        # within 30 days
        "acceptance": RiskLevel.LOW,      # final acceptance 20 days after
        "penalty": RiskLevel.MEDIUM,      # 1% per week
        "liability_cap": RiskLevel.MEDIUM,  # 100% of subcontract price
        "ip": RiskLevel.HIGH,             # work-for-hire vests one-way
        "data_protection": RiskLevel.LOW, # GDPR processor
        "exclusivity": RiskLevel.MEDIUM,
        "termination": RiskLevel.MEDIUM,  # immediate upon default
    },
    "sponsorship_clean.pdf": {
        "price": RiskLevel.LOW,           # USD 50,000
        "payment": RiskLevel.LOW,         # within 30 days
        "duration": RiskLevel.LOW,        # 12 months
        "termination": RiskLevel.LOW,
    },
    "employment_offer.docx": {
        "duration": RiskLevel.MEDIUM,     # 2 years
        "price": RiskLevel.LOW,           # USD 180,000 salary
        "confidentiality": RiskLevel.MEDIUM,  # survives termination
        "ip": RiskLevel.HIGH,             # inventions assigned one-way
        "termination": RiskLevel.LOW,     # for cause
        "exclusivity": RiskLevel.MEDIUM,  # non-compete 12 months
        "data_protection": RiskLevel.LOW,
    },
    "loan_term_high.docx": {
        "price": RiskLevel.LOW,           # EUR 1,500,000
        "duration": RiskLevel.HIGH,       # loan term 10 years
        "penalty": RiskLevel.MEDIUM,      # late-payment penalty 5%/month
        "payment": RiskLevel.LOW,         # instalment schedule
    },
    "joint_venture.docx": {
        "duration": RiskLevel.MEDIUM,     # joint venture 5 years
        "price": RiskLevel.LOW,           # contributions
        "ip": RiskLevel.LOW,              # each party retains
        "termination": RiskLevel.LOW,
        "confidentiality": RiskLevel.LOW,
    },
    "event_security.docx": {
        "quantity": RiskLevel.LOW,        # 12 events
        "price": RiskLevel.LOW,           # USD 8,500 / USD 102,000
        "delivery": RiskLevel.MEDIUM,     # at least 5 days before (aggressive)
        "payment": RiskLevel.LOW,         # net 30 days
        "acceptance": RiskLevel.LOW,      # acceptance inspection
        "liability_cap": RiskLevel.MEDIUM,  # indemnification + cap
        "confidentiality": RiskLevel.LOW,
    },
    "outsole_buyback.docx": {
        "duration": RiskLevel.MEDIUM,     # initial 3 years
        "exclusivity": RiskLevel.HIGH,    # sole distributor
        "payment": RiskLevel.MEDIUM,      # within 90 days
        "renewal": RiskLevel.MEDIUM,      # auto-renew 3 years
    },
    "mfs_terms.docx": {
        "price": RiskLevel.LOW,           # USD 990
        "data_protection": RiskLevel.LOW, # DPA / SCCs
        "liability_cap": RiskLevel.MEDIUM,  # limited to 3 months fees
        "duration": RiskLevel.MEDIUM,     # 24 months
        "renewal": RiskLevel.MEDIUM,      # auto-renews
    },
    "consulting_high_penalty.docx": {
        "price": RiskLevel.LOW,           # USD 400,000
        "payment": RiskLevel.MEDIUM,      # 50% on signing
        "penalty": RiskLevel.MEDIUM,      # 3% per week
        "ip": RiskLevel.HIGH,             # work product assigned one-way
        "exclusivity": RiskLevel.MEDIUM,  # no-compete services 2 years
        "confidentiality": RiskLevel.LOW,
    },
    "office_lease.docx": {
        "duration": RiskLevel.MEDIUM,     # initial term 5 years
        "price": RiskLevel.LOW,           # USD 48,000
        "penalty": RiskLevel.MEDIUM,      # late rent 5%/month
        "renewal": RiskLevel.MEDIUM,      # auto-renew
    },
    "ad_agency_msa.docx": {
        "price": RiskLevel.LOW,           # retainer USD 15,000
        "payment": RiskLevel.LOW,         # net 30 days
        "acceptance": RiskLevel.MEDIUM,   # within 10 business days
        "ip": RiskLevel.MEDIUM,           # assigned upon full payment
        "renewal": RiskLevel.MEDIUM,      # renews quarterly
        "confidentiality": RiskLevel.LOW,
    },
    "early_termination_clean.docx": {
        "duration": RiskLevel.LOW,        # 12 months
        "price": RiskLevel.LOW,           # USD 36,000
        "termination": RiskLevel.LOW,     # either party, convenience
        "payment": RiskLevel.LOW,         # net 30 days
        "confidentiality": RiskLevel.LOW,
    },
}


def _max_risk(items: list[RiskLevel]) -> RiskLevel:
    return max(items, key=lambda r: _RISK_ORDER[r]) if items else RiskLevel.LOW


def run_fixture_suite(out_stream=None) -> dict:
    """Run the rule layer over every fixture and score AC1/AC2.

    Returns a metrics dict; prints a human-readable report to out_stream
    (defaults to stdout).
    """
    if out_stream is None:
        out_stream = sys.stdout

    fixtures = sorted(p for p in FIXTURE_DIR.iterdir() if p.is_file())
    results: dict[str, dict[str, RiskLevel]] = {}

    for path in fixtures:
        result = extract_document(str(path))
        per_field: dict[str, list[RiskLevel]] = {}
        for clause in result.clauses:
            per_field.setdefault(clause.clause, []).append(clause.risk)
        results[path.name] = {k: _max_risk(v) for k, v in per_field.items()}

    # --- scoring ---
    total_expected = 0
    found = 0
    recall_missed: list[tuple[str, str]] = []
    risk_correct = 0
    risk_total = 0
    risk_wrong: list[tuple[str, str, RiskLevel, RiskLevel]] = []
    doc_risk_total = 0
    doc_risk_correct = 0

    for path in fixtures:
        name = path.name
        expected = EXPECTED.get(name)
        if expected is None:
            continue
        got = results.get(name, {})
        expected_doc = _max_risk(list(expected.values()))
        got_doc = _max_risk(list(got.values()))
        doc_risk_total += 1
        if got_doc == expected_doc:
            doc_risk_correct += 1
        extra = sorted(set(got) - set(expected))
        for field, risk in sorted(expected.items()):
            total_expected += 1
            hits = got.get(field)
            if not hits:
                recall_missed.append((name, field))
                continue
            found += 1
            risk_total += 1
            if hits == risk:
                risk_correct += 1
            else:
                risk_wrong.append((name, field, risk, hits))
        gap = f"  [extra: {', '.join(extra)}]" if extra else ""
        print(
            f"  {name:38s} doc_risk={got_doc.value:6s} fields={len(got):2d}{gap}",
            file=out_stream,
        )

    recall = found / total_expected if total_expected else 1.0
    accuracy = risk_correct / risk_total if risk_total else 1.0
    doc_acc = doc_risk_correct / doc_risk_total if doc_risk_total else 1.0

    print(file=out_stream)
    print(f"expected fields: {total_expected}   found: {found}   recall: {recall:.1%}  (AC1 >= 95%)", file=out_stream)
    print(f"risk accuracy:   {risk_correct}/{risk_total} = {accuracy:.1%}  (AC2 >= 90%)", file=out_stream)
    print(f"document risk:   {doc_risk_correct}/{doc_risk_total} = {doc_acc:.1%}", file=out_stream)
    if recall_missed:
        print(file=out_stream)
        print("recall misses (expected field never extracted):", file=out_stream)
        for name, field in recall_missed:
            print(f"  {name}: {field}", file=out_stream)
    if risk_wrong:
        print(file=out_stream)
        print("risk mismatches (full_name expected got):", file=out_stream)
        for name, field, want, got_v in risk_wrong:
            print(f"  {name}: {field} expected={want.value} got={got_v.value}", file=out_stream)

    return {
        "expected": total_expected,
        "found": found,
        "recall": recall,
        "risk_correct": risk_correct,
        "risk_total": risk_total,
        "risk_accuracy": accuracy,
        "doc_risk_correct": doc_risk_correct,
        "doc_risk_total": doc_risk_total,
    }


if __name__ == "__main__":
    runner = run_fixture_suite(sys.stdout)
    raise SystemExit(0 if (runner["recall"] >= 0.95 and runner["risk_accuracy"] >= 0.90) else 1)
