"""SPEC acceptance-criteria regression tests over the fixture corpus.

AC1  per-field recall    >= 95%
AC2  risk-flag accuracy  >= 90%
AC3  every clause carries clause/page/quote/risk levels
AC4  offline rule layer produces structurally complete output
"""

from __future__ import annotations

import io

from contract_extract.eval_fixtures import EXPECTED, run_fixture_suite


def test_acceptance_criteria_hold():
    metrics = run_fixture_suite(out_stream=io.StringIO())
    assert metrics["recall"] >= 0.95, f"AC1 recall {metrics['recall']:.1%} < 95%"
    assert metrics["risk_accuracy"] >= 0.90, (
        f"AC2 risk accuracy {metrics['risk_accuracy']:.1%} < 90%"
    )


def test_offline_extraction_is_structurally_complete(sample_pdf, sample_docx):
    from contract_extract.pipeline import extract_document

    for path in (sample_pdf, sample_docx):
        r = extract_document(str(path))
        assert r.engine == "rules"
        assert r.risk.value in {"LOW", "MEDIUM", "HIGH"}
        assert r.pages >= 1
        for c in r.clauses:
            # AC3: every output carries clause name, page, quote, risk level.
            assert c.clause
            assert isinstance(c.page, int) and c.page >= 1
            assert len(c.quote) > 0
            assert c.risk.value in {"LOW", "MEDIUM", "HIGH"}


def test_every_fixture_expected_table_is_covered(contract_dir):
    files = [p.name for p in contract_dir.glob("*") if p.suffix.lower() in (".pdf", ".docx")]
    missing = [f for f in files if f not in EXPECTED]
    unexpected = [k for k in EXPECTED if k not in files]
    assert not missing, f"fixtures missing from EXPECTED table: {missing}"
    assert not unexpected, f"EXPECTED entries have no fixture: {unexpected}"
