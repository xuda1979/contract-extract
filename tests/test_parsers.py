"""Document loading: PDF and DOCX parsing tests (SPEC item 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from contract_extract.errors import UnsupportedFormatError
from contract_extract.parsers import load_document


def test_pdf_loads_with_pages_and_format(sample_pdf):
    doc = load_document(sample_pdf)
    assert doc.format == "pdf"
    assert doc.nonempty_pages >= 1
    text = doc.text
    # Content from the supply fixture.
    assert "Supply Agreement" in text
    assert "10,000 units" in text


def test_docx_loads_with_format(sample_docx):
    doc = load_document(sample_docx)
    assert doc.format == "docx"
    assert doc.nonempty_pages >= 1
    assert "Employment" in doc.text


def test_unsupported_extension_raises(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        load_document(f)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):  # (or IsADirectoryError on a dir)
        load_document(tmp_path / "does_not_exist.pdf")


def test_page_number_of_finds_correct_page(sample_pdf):
    doc = load_document(sample_pdf)
    # A sentence that must exist on page 1.
    page = doc.page_number_of("Supply Agreement")
    assert page == 1
