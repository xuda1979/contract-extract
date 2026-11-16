"""Pipeline, offline fallback and schema model tests."""

from __future__ import annotations

import json

from contract_extract.pipeline import extract_document, get_provider, is_llm_available
from contract_extract.rules.engine import document_risk
from contract_extract.schema import DocumentResult, RiskLevel


def test_schema_round_trip(sample_pdf):
    r = extract_document(str(sample_pdf))
    data = r.model_dump(mode="json")
    # Round-trip through the pydantic model validates the JSON contract.
    again = DocumentResult.model_validate(data)
    assert again == r
    assert again.source == str(sample_pdf)
    assert again.format == "pdf"


def test_document_risk_aggregation():
    from contract_extract.schema import ExtractedClause

    low = ExtractedClause(clause="x", page=1, quote="q", risk=RiskLevel.LOW)
    med = ExtractedClause(clause="x", page=1, quote="q", risk=RiskLevel.MEDIUM)
    high = ExtractedClause(clause="x", page=1, quote="q", risk=RiskLevel.HIGH)
    assert document_risk([low]) == RiskLevel.LOW
    assert document_risk([med]) == RiskLevel.MEDIUM
    assert document_risk([high]) == RiskLevel.HIGH
    assert document_risk([med, high]) == RiskLevel.HIGH
    assert document_risk([]) == RiskLevel.LOW


def test_offline_llm_request_degrades_to_rules(sample_docx):
    # AC4: LLM closed must still be fully usable — the optional enhancement
    # must not change (or break) the rule results.
    r_no_llm = extract_document(str(sample_docx), use_llm=False)
    r_llm = extract_document(str(sample_docx), use_llm=True)
    assert r_llm.engine == "rules"
    assert r_llm.clauses == r_no_llm.clauses
    assert not is_llm_available()


def test_offline_provider_is_noop():
    p = get_provider()
    assert p.active() is False


def test_json_schema_is_valid(model_json=DocumentResult.model_json_schema()):
    # Pydantic model_json_schema must be JSON-serialisable and a dict.
    assert isinstance(model_json, dict)
    json.dumps(model_json)
