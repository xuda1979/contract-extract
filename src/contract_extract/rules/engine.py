"""Rule-based extraction engine.

The engine walks a loaded document sentence by sentence (page-aware),
applies every clause rule in the library, dedupes and returns
`ExtractedClause` objects.  This layer always works — LLM is optional.
"""

from __future__ import annotations

import re

from contract_extract.parsers import Document
from contract_extract.rules.match import Match
from contract_extract.schema import ExtractedClause, RiskLevel
from contract_extract.rules import library

_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?\u3002\uff01\uff1f\uff1b])\s+(?=[0-9A-Z\u4e00-\u9fff\"'\u2018\u201c(\[)"
    r";\s+(?=[A-Z0-9\u4e00-\u9fff])"
)


def iter_sentences(doc: Document):
    """Yield (page_number, sentence_text) for every page of a document."""
    for page in doc.pages:
        for paragraph in re.split(r"\n\s*\n", page.text):
            for sentence in _SENTENCE_SPLIT.split(paragraph):
                sentence = sentence.strip()
                if not sentence:
                    continue
                if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", sentence):
                    continue
                if re.fullmatch(r"[0-9A-Za-z.]{1,4}", sentence):
                    continue  # bare numbering / fragments
                yield page.page_number, sentence


def run_rules(doc: Document) -> list[ExtractedClause]:
    """Run every clause rule over a loaded document."""
    sentences = list(iter_sentences(doc))
    matches: list[Match] = []
    for rule_name, rule_fn in sorted(library.RULES.items()):
        try:
            matches.extend(rule_fn(doc, sentences))
        except Exception:
            # A single bad rule must never crash the whole extraction.
            continue

    return _dedupe(matches)


def _dedupe(matches: list[Match]) -> list[ExtractedClause]:
    seen: set[tuple[str, int, str]] = set()
    out: list[ExtractedClause] = []
    for m in matches:
        key = (m.rule, m.page, m.sentence[:80])
        if key in seen:
            continue
        seen.add(key)
        quote = m.sentence
        if len(quote) > 260:
            quote = quote[:257].rstrip() + "..."
        out.append(
            ExtractedClause(
                clause=m.rule,
                page=m.page,
                quote=quote,
                risk=m.risk,
                flag=m.flag,
                detail=m.detail,
            )
        )
    return out


def document_risk(clauses: list[ExtractedClause]) -> RiskLevel:
    """Aggregate per-document risk from its clauses."""
    if not clauses:
        return RiskLevel.LOW
    if any(c.risk == RiskLevel.HIGH for c in clauses):
        return RiskLevel.HIGH
    if any(c.risk == RiskLevel.MEDIUM for c in clauses):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
