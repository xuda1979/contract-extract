# 06 · Contract-Extract — 合同/标书风险抽取

Extract risk clauses and key information from contracts and bid documents
(PDF / DOCX) into a structured JSON result plus a human-readable review
checklist.  Two-layer design:

1. **Rule layer** — always available, runs entirely locally (no cloud).
2. **LLM enhancement layer** — optional, per-use pay-as-you-go, falls back to
   the rule layer when disabled (`LLM_OFFLINE=1`).

This is a vertical slice in the Adobe Acrobat / Document Cloud direction and is
aimed at solo operators (SMEs / lawyers / procurement).

## Features

- Loads **PDF** locally (`pdfplumber` + `pypdf` fallback) and **DOCX**
  (`python-docx`).
- Extracts **15 clause fields** with risk flags:

  `duration` · `quantity` · `price` · `delivery` · `acceptance` · `payment` ·
  `penalty` · `liability_cap` · `confidentiality` · `ip` · `renewal` ·
  `termination` · `exclusivity` · `data_protection` · `insurance`

- Every extracted clause carries: **clause name, page number, original quote,
  risk level** (`LOW` / `MEDIUM` / `HIGH`).
- **Structured output**: one `DocumentResult` JSON object per document
  (JSON Schema via pydantic, `--schema`).
- **Offline-first**: with `LLM_OFFLINE=1` (or no API key) the rule layer alone
  is complete and testable.
- **Checklist**: Markdown review checklist per document.
- **CLI + Web demo**.
- **Batch** directory processing with streaming output (flat memory on 100+
  files).

## Install

Project uses Python ≥ 3.11.  No build step is required to run from the repo:

```bash
# From the repo root
PYTHONPATH=src python -m contract_extract --version
```

To install as a package:

```bash
python -m pip install -e .          # plus: pip install -e ".[llm]" for LLM
```

Bundled dependencies: `pydantic`, `pypdf`, `pdfplumber`, `python-docx`.

## Usage

### CLI

```bash
# Single document -> NDJSON to stdout
PYTHONPATH=src python -m contract_extract path/to/contract.pdf

# Batch a whole directory (pdf/docx, recursive)
PYTHONPATH=src python -m contract_extract ./contracts/ -o results.ndjson

# Also render one Markdown review checklist per document
PYTHONPATH=src python -m contract_extract ./contracts/ -o results.ndjson --checklist

# JSON Schema of the output contract
PYTHONPATH=src python -m contract_extract --schema

# Fixture acceptance suite (recall / risk accuracy)
PYTHONPATH=src python -m contract_extract --fixtures
```

Example output shape (`results.ndjson`, line-delimited):

```json
{"source": "a.pdf", "format": "pdf", "pages": 3, "risk": "HIGH",
 "clauses": [
   {"clause": "penalty", "page": 2, "quote": "…liquidated damages of 30%…",
    "risk": "HIGH", "flag": "penalty 30% (onerous)", "detail": "30%"},
   {"clause": "liability_cap", "page": 1, "quote": "…disclaims all warranties…",
    "risk": "HIGH", "flag": "party disclaims liability (broad)", "detail": null}
 ], "engine": "rules", "generated_at": "2026-11-16", "warnings": []}
```

### Web demo

```bash
PYTHONPATH=src python -m contract_extract.web     # http://127.0.0.1:8000/
# HOST=0.0.0.0 PORT=8080 to expose; uploads are processed in /tmp and deleted.
```

### LLM enhancement

On by default is **off**.  Enable per-run with `--llm`:

```bash
export OPENAI_API_KEY=sk-...          # gpt-4o-mini by default (OPENAI_MODEL)
PYTHONPATH=src python -m contract_extract agreement.pdf --llm
export ANTHROPIC_API_KEY=sk-ant-...   # claude-3-haiku by default (ANTHROPIC_MODEL)
```

The LLM layer only adds clauses the rule layer did not already find and
never deletes rule results.  If the call fails, the rule results are kept.
Set `LLM_OFFLINE=1` to force the offline path even when a key is present.

### Model-call cost notes

The enhancement prompt sends at most the first **3 pages**, **900 chars/page**,
and asks for a small JSON object (`max_tokens=1500`; request/response token
usage only on those pages).  Costs are therefore small and *per document*;
estimate roughly `gpt-4o-mini` ≈ <US$0.01/completion ask and Anthropic
`claude-3-haiku` ≈ a similar order of magnitude at the time of writing.
Because the rule layer runs first, you can also choose to skip `--llm`
entirely and stay at zero API cost.

## Acceptance criteria

| AC | Requirement | Status |
|----|-------------|--------|
| 1  | 20-fixture corpus, per-field recall ≥ 95% | **100%** |
| 2  | risk-flag accuracy ≥ 90% (expert baseline) | **100%** |
| 3  | every clause: name · page · quote · risk | ✓ (pydantic schema) |
| 4  | offline (`LLM_OFFLINE=1`) still complete | ✓ (CLI + tests) |
| 5  | batch 100+ PDFs without memory blowup | ✓ (streaming NDJSON) |
| 6  | tests + README + sample output | ✓ |

Run the suite:

```bash
PYTHONPATH=src LLM_OFFLINE=1 python -m pytest tests/ -q
PYTHONPATH=src python -m contract_extract --fixtures
```

## Repository layout

```
src/contract_extract/
  parsers/     PDF / DOCX text extraction
  rules/       offline clause rules + engine + match
  llm/         optional OpenAI / Anthropic providers (offline no-op)
  pipeline.py  single-document orchestration
  batch.py     streaming NDJSON batch processor
  cli.py, web.py   CLI and stdlib-only web demo
  schema.py    pydantic output models (JSON Schema)
  checklist.py Markdown review checklist
  eval_fixtures.py  fixture acceptance scorer
fixtures/generate_fixtures.py   deterministic fixture generator (20 docs)
reports/baseline.txt            rule-layer baseline output
```

## Limitations / non-goals

- No e-signature support.
- No full arbitration/litigation legal analysis — this is an assistive review
  tool, not legal advice.
- Deep semantic parsing of non-contract documents (e.g. invoices) is out of
  scope.

## License

Dual-licensed: the source code is released under the
**GNU Affero General Public License v3.0 (AGPL-3.0)** — see
[`LICENSE`](LICENSE). Any commercial or profit-making use, or use in a
product or service sold or licensed to others, requires a separate
written commercial authorization agreement with the copyright holder — see
[`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md).

Copyright (c) 2026 许达 (Xu Da) / 中国移动研究院 (China Mobile Research Institute).
