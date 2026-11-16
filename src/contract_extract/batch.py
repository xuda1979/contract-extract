"""Streaming batch processor: no loading of 100 results into memory.

Each document is processed and its JSON line flushed to the NDJSON output
stream immediately (per-document streaming).  Only one document's text and
clauses are in memory at a time.  Memory stays flat on 100+ files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from contract_extract.pipeline import extract_document
from contract_extract.schema import DocumentResult


def iter_documents(paths: list[Path]) -> list[Path]:
    """Expand directories into sorted pdf/docx file paths."""
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files = sorted(
                [f for f in p.rglob("*") if f.suffix.lower() in (".pdf", ".docx")],
                key=lambda f: str(f).lower(),
            )
            out.extend(files)
        elif p.suffix.lower() in (".pdf", ".docx"):
            out.append(p)
        else:
            print(f"skip: unsupported {p}", file=sys.stderr)
    return out


def run_batch(
    paths: list[Path],
    out_stream,
    use_llm: bool = False,
    progress: callable | None = None,
    include_clauses: bool = True,
) -> dict:
    """Process documents and stream NDJSON lines to `out_stream`.

    Returns a small summary dict (not the results themselves, which are
    streamed, avoiding memory blowup).
    """
    docs = iter_documents(paths)
    total = len(docs)
    processed = 0
    failed: list[tuple[str, str]] = []

    for idx, path in enumerate(docs):
        try:
            result: DocumentResult = extract_document(str(path), use_llm=use_llm)
        except Exception as exc:
            failed.append((str(path), repr(exc)))
            continue
        if progress:
            progress(idx + 1, total, str(path))
        payload = result.model_dump(mode="json")
        if not include_clauses:
            payload.pop("clauses", None)
        out_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        processed += 1

    return {"documents": processed, "failed": failed, "total": total}


def write_pretty_json(path: str, results: list[DocumentResult]) -> None:
    """Write a single consolidated pretty JSON file (used by tests/demo)."""
    data = [r.model_dump(mode="json") for r in results]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"documents": data}, fh, ensure_ascii=False, indent=2)
