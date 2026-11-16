"""End-to-end extraction pipeline for a single document.

load -> (rules + optional LLM) -> DocumentResult.  LLM is opt-in and falls
back to the rule layer when offline (LLM_OFFLINE=1 or no API keys).
"""

from __future__ import annotations

import os
from datetime import date

from contract_extract.errors import OfflineModeError
from contract_extract.llm.provider import LLMProvider, NoopLLM
from contract_extract.parsers import load_document
from contract_extract.rules.engine import document_risk, run_rules
from contract_extract.schema import DocumentResult, ExtractedClause

_ENABLED = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
}


def is_llm_available() -> bool:
    """LLM is on only when not forced offline and a key is configured."""
    if os.environ.get("LLM_OFFLINE", "0") in ("1", "true", "yes", "on"):
        return False
    return any(os.environ.get(k) for k in _ENABLED)


def get_provider() -> LLMProvider:
    if not is_llm_available():
        return NoopLLM()
    if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        from contract_extract.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    from contract_extract.llm.openai_provider import OpenAIProvider

    return OpenAIProvider()


def extract_document(
    path: str,
    source_format: str | None = None,
    use_llm: bool = False,
    title: str | None = None,
    progress: callable | None = None,
) -> DocumentResult:
    """Extract clauses from a single pdf/docx file.

    `use_llm=True` asks the optional enhancement layer to also run; it is a
    no-op (and never required) when offline.
    """
    doc = load_document(path, progress=progress)

    clauses = run_rules(doc)
    engine = "rules"

    if use_llm:
        provider = get_provider()
        if provider is None or not provider.active():
            # Offline/LLM_OFFLINE: the optional layer is a no-op and the rule
            # results already extracted are returned as-is (engine stays "rules").
            pass
        else:
            try:
                extra = provider.enhance(doc, clauses)
                if extra:
                    clauses = _merge_clauses(clauses, extra)
                    engine = "rules+llm"
            except OfflineModeError:
                raise
            except Exception:
                pass  # If the LLM call fails we keep the rule results.

    result = DocumentResult(
        source=path,
        format=source_format or doc.format,
        title=title,
        pages=doc.nonempty_pages,
        risk=document_risk(clauses),
        clauses=clauses,
        engine=engine,
        generated_at=date.today(),
        warnings=list(doc.warnings),
    )
    return result


def _merge_clauses(base: list[ExtractedClause], extra: list[ExtractedClause]) -> list[ExtractedClause]:
    seen = {(c.clause) for c in base}
    merged = list(base)
    for c in extra:
        if c.clause in seen:
            continue  # rules already found this field
        seen.add(c.clause)
        merged.append(c)
    return merged
