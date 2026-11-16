"""LLM provider interface + offline no-op implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from contract_extract.rules.engine import document_risk
from contract_extract.rules.library import rule_duration, RULES  # noqa: F401  (for templates)
from contract_extract.schema import ExtractedClause
from contract_extract.schema import RiskLevel


class LLMProvider(ABC):
    """Base class for pay-per-use LLM enhancement providers."""

    @abstractmethod
    def enhance(self, doc, clauses) -> list[ExtractedClause] | None:
        """Extract clauses that rules could not.  Return None when nothing new."""
        ...

    @abstractmethod
    def active(self) -> bool:
        """Whether the provider can actually call the API right now."""
        ...


class NoopLLM(LLMProvider):
    """Always-off provider.  `enhance` re-runs the rule layer (a template
    fallback) so the pipeline remains structurally complete offline."""

    def enhance(self, doc, clauses):
        # template fallback: re-run rules deterministically.
        from contract_extract.rules.engine import run_rules

        base = run_rules(doc)
        seen = {c.clause for c in clauses}
        new = [c for c in base if c.clause not in seen]
        return new or None

    def active(self) -> bool:
        return False
