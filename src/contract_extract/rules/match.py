"""Shared match object between the rule library and the engine."""

from __future__ import annotations

from dataclasses import dataclass

from contract_extract.schema import RiskLevel


@dataclass
class Match:
    """One rule-based hit.  The engine converts these to ExtractedClause."""

    rule: str
    page: int
    sentence: str
    risk: RiskLevel
    flag: str | None = None
    detail: str | None = None
