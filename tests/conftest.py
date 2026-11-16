"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


@pytest.fixture()
def contract_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Rule layer only by default: tests never hit a paid API."""
    monkeypatch.setenv("LLM_OFFLINE", "1")


@pytest.fixture()
def sample_pdf(contract_dir) -> Path:
    return contract_dir / "supply_agreement_clean.pdf"


@pytest.fixture()
def sample_docx(contract_dir) -> Path:
    return contract_dir / "employment_offer.docx"
