"""CLI entry point: single file, batch directory, markdown checklist,
JSON-Schema dump, run tests on fixtures, and offline verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contract_extract.batch import run_batch
from contract_extract.checklist import FIELD_LABELS, build_checklist
from contract_extract.pipeline import extract_document, is_llm_available
from contract_extract.schema import DocumentResult
from contract_extract.__init__ import __version__


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="contract-extract",
        description="Extract risk clauses from contracts / bids (PDF/DOCX).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("paths", nargs="*", help="Files or directories to process.")
    p.add_argument("--llm", action="store_true", help="Enable optional LLM enhancement (offline-safe, off by default).")
    p.add_argument("--llm-embed", dest="llm_embed", action="store_true", help="Alias of --llm.")
    p.add_argument("--checklist", action="store_true", help="Also write a Markdown review checklist for each doc.")
    p.add_argument("--schema", action="store_true", help="Print JSON-Schema to stdout and exit.")
    p.add_argument("--version", action="store_true", help="Print version and exit.")
    p.add_argument("--output", "-o", default=None, help="Write NDJSON to FILE instead of stdout.")
    p.add_argument("--fixtures", action="store_true", help="Run the fixture corpus and print per-field recall.")
    return p.parse_args(argv)


def _schema_text() -> str:
    from contract_extract.schema import DocumentResult

    return json.dumps(
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "DocumentResult",
            "description": "Contract risk-clause extraction output for one document.",
            **_nested(DocumentResult),
        },
        ensure_ascii=False,
        indent=2,
    )


def _nested(model):
    return model.model_json_schema()


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.version:
        print(f"contract-extract {__version__}")
        return 0
    if args.schema:
        print(_schema_text())
        return 0
    if args.fixtures:
        from contract_extract.eval_fixtures import run_fixture_suite

        run_fixture_suite(sys.stdout)
        return 0

    if not args.paths:
        print("error: provide at least one file/directory", file=sys.stderr)
        return 2

    out_stream = sys.stdout
    close_out = False
    if args.output:
        out_stream = open(args.output, "w", encoding="utf-8")
        close_out = True

    if args.llm and not is_llm_available():
        print(
            "note: LLM enhancement requested but the environment is offline "
            "(LLM_OFFLINE=1 or no API key). Falling back to the rule layer.",
            file=sys.stderr,
        )

    try:
        summary = run_batch(
            [Path(p) for p in args.paths],
            out_stream,
            use_llm=bool(args.llm or args.llm_embed),
        )
        if summary["total"] == 0:
            print(
                "error: no .pdf/.docx files found in the given paths",
                file=sys.stderr,
            )
            return 2
        if args.checklist:
            # Re-read each NDJSON file (small) to render markdown.
            if args.output:
                out_stream.flush()
                _write_checklists_from_ndjson(args.output)
            else:
                print(
                    "note: --checklist needs --output FILE to write .md next to the JSON.",
                    file=sys.stderr,
                )
        if summary["failed"]:
            for p, e in summary["failed"]:
                print(f"failed: {p}: {e}", file=sys.stderr)
        print(
            f"processed {summary['documents']}/{summary['total']} documents",
            file=sys.stderr,
        )
        return 1 if summary["failed"] else 0
    finally:
        if close_out:
            out_stream.close()


def _write_checklists_from_ndjson(path: str) -> None:
    ndjson = Path(path)
    docs_dir = ndjson.parent / (ndjson.stem + "_checklists")
    docs_dir.mkdir(parents=True, exist_ok=True)
    for line in ndjson.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except Exception:
            continue
        r = DocumentResult.model_validate(data)
        md = docs_dir / (Path(data["source"]).stem + ".md")
        md.write_text(build_checklist(r), encoding="utf-8")
        print(f"  checklist -> {md}")


if __name__ == "__main__":
    sys.exit(main())
