"""Rule utilities: regex-based matches, numbering, dedupe, risk levels."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from contract_extract.schema import RiskLevel

if TYPE_CHECKING:
    from contract_extract.rules.match import Match

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in '"\u201c\u201d' and s[-1] in '"\u201c\u201d':
        s = s[1:-1]
    return s.strip()


LEADING_MARKER_RE = re.compile(r"^\s*(?:\(\d+\)|\d+\.?|[ivxlcdm]+\.?|[A-Z]\)?)?\s*")


def clean_marker(text: str) -> str:
    """Remove leading clause/hard numbered markers for tidier flags/quotes."""
    return LEADING_MARKER_RE.sub("", text).strip()


def has_negation(text: str) -> bool:
    """Hedges that weaken a hard dollar/date/percent statement."""
    return any(
        w in text.lower()
        for w in (
            "not apply",
            "no limit",
            "not limit",
            "unlimited",
            "uncapped",
            "no cap",
            "no monetary",
            "without limitation",
        )
    )


def is_negative_sentence(text: str, words: Iterable[str]) -> bool:
    """True when the sentence already contains a negation about `words`."""
    lower = text.lower()
    return any(m in lower for m in ("no", "not", "never", "without", "exclu", "does not", "shall not"))


def compact(text: str) -> str:
    """Compact whitespace for tests/audit output."""
    return " ".join(text.split())


TOPPED = ("cap", "limit", "maximum", "max of", "up to", "at most", "ceiling", "not exceed")
BOTTOM = ("minimum", "min of", "at least", "floor", "lowest", "starting from", "not less")


def contains_top(words: list[str]) -> bool:
    return any(w in words for w in TOPPED)


def contains_bottom(words: list[str]) -> bool:
    return any(w in words for w in BOTTOM)


def risk_mix(clauses: Iterable[Match]) -> RiskLevel:
    """Aggregate risk over a list of clauses."""
    levels = [c.risk for c in clauses]
    if not levels:
        return RiskLevel.LOW
    if RiskLevel.HIGH in levels:
        return RiskLevel.HIGH
    if RiskLevel.MEDIUM in levels:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
