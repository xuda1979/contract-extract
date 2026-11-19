"""CLI tests: schema dump, version, single-file NDJSON, checklist output."""

from __future__ import annotations

import json
from pathlib import Path

from contract_extract.cli import _schema_text, main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "contract-extract" in capsys.readouterr().out


def test_schema_dump_is_valid_json(capsys):
    assert main(["--schema"]) == 0
    out = capsys.readouterr().out
    schema = json.loads(out)
    assert schema["title"] == "DocumentResult"
    assert "properties" in schema


def test_single_file_ndjson(sample_pdf, capsys):
    rc = main([str(sample_pdf)])
    assert rc == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["format"] == "pdf"
    assert len(payload["clauses"]) > 0


def test_missing_args(capsys):
    assert main([]) == 2


def test_no_supported_files_exits_error(tmp_path, capsys):
    rc = main([str(tmp_path)])
    assert rc == 2
    assert "no .pdf/.docx files found" in capsys.readouterr().err


def test_fixtures_mode_needs_no_paths(capsys):
    assert main(["--fixtures"]) == 0
    out = capsys.readouterr().out
    assert "recall" in out.lower()
    assert "AC1" in out


def test_checklist_writes_markdown(sample_pdf, tmp_path, capsys):
    out_file = tmp_path / "out.ndjson"
    rc = main(["--checklist", "-o", str(out_file), str(sample_pdf)])
    assert rc == 0
    capsys.readouterr()
    checkdir = tmp_path / "out_checklists"
    md_files = list(checkdir.glob("*.md"))
    assert md_files
    text = md_files[0].read_text(encoding="utf-8")
    assert "Contract Review Checklist" in text
    assert "overall risk" in text.lower()


def test_checked_in_schema_matches_live_output():
    # The schema/ tree is the documented authoritative downstream contract
    # (see schema.py docstring and the manual). It must not drift from
    # `contract-extract --schema`.
    repo = Path(__file__).resolve().parents[1]
    checked_in = json.loads(
        (repo / "schema" / "documented_result.schema.json").read_text(encoding="utf-8")
    )
    assert checked_in == json.loads(_schema_text())
