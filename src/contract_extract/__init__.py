"""contract_extract: contract / bid risk clause extraction (PDF/DOCX).

Two-layer extraction: local rule-based layer (always available) plus an
optional pay-per-use LLM enhancement layer (off by default, disabled when
LLM_OFFLINE=1, falls back to the rule layer).
"""

__version__ = "0.1.0"

from contract_extract.schema import (
    RiskLevel,
    ExtractedClause,
    DocumentResult,
    BatchResult,
)

__all__ = [
    "RiskLevel",
    "ExtractedClause",
    "DocumentResult",
    "BatchResult",
    "__version__",
]
