"""CLI tests: schema dump, version, single-file NDJSON, checklist output."""

from __future__ import annotations

import json

from contract_extract.cli import main


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
