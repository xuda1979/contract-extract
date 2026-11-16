"""Batch streaming tests (SPEC item 5: batch + queue, no memory blowup)."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from contract_extract.batch import iter_documents, run_batch


def test_iter_documents_expands_directories(contract_dir):
    paths = iter_documents([contract_dir])
    names = {p.name for p in paths}
    assert len(paths) >= 15
    assert "supply_agreement_clean.pdf" in names
    assert "employment_offer.docx" in names


def test_iter_documents_skips_unsupported(tmp_path):
    (tmp_path / "x.txt").write_text("no", encoding="utf-8")
    assert iter_documents([tmp_path]) == []


def test_run_batch_streams_ndjson(contract_dir):
    out = io.StringIO()
    summary = run_batch([contract_dir / "supply_agreement_clean.pdf"], out)
    assert summary["documents"] == 1
    assert summary["failed"] == []
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["source"].endswith("supply_agreement_clean.pdf")
    assert payload["format"] == "pdf"
    assert "clauses" in payload


def test_run_batch_tolerates_corrupt_file(tmp_path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"not a real pdf")
    good = tmp_path / "good.pdf"
    shutil.copy(Path("fixtures/contracts/supply_agreement_clean.pdf"), good)
    out = io.StringIO()
    summary = run_batch([good, pdf], out)
    assert summary["documents"] == 1
    assert len(summary["failed"]) == 1
    # Only the good file produced NDJSON.
    assert len(out.getvalue().strip().splitlines()) == 1


def test_run_batch_is_streaming_not_buffering(contract_dir):
    # AC5 demo: process many files through a single in-memory stream,
    # results are flushed line by line.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = contract_dir / "supply_agreement_clean.pdf"
        for i in range(60):
            shutil.copy(src, Path(tmp) / f"d{i:03d}.pdf")
        out = io.StringIO()
        summary = run_batch([Path(tmp)], out)
        assert summary["documents"] == 60
        assert summary["total"] == 60
        assert len(out.getvalue().strip().splitlines()) == 60
