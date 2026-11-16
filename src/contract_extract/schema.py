"""Pydantic output models: the JSON-Schema contract of the extractor.

One JSON file per input document (one JSON *object* per document):
DocumentResult. Batch mode emits a JSON *array* of DocumentResult, one per
input document.  Use `DocumentResult.model_json_schema()` to obtain the JSON
Schema (also written to `schema/documented_result.schema.json` and exposed by
the CLI with `--schema`).
"""

from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExtractedClause(BaseModel):
    """One extracted risk clause."""

    clause: str = Field(..., description="Clause name, e.g. 'termination'.")
    page: int = Field(..., description="1-based page number where found.")
    quote: str = Field(..., description="Original text snippet.")
    risk: RiskLevel = Field(..., description="Risk flag: LOW/MEDIUM/HIGH.")
    flag: Optional[str] = Field(
        None, description="Optional human-readable risk flag, e.g. 'no cap on liability'."
    )
    detail: Optional[str] = Field(None, description="Optional normalised detail.")


class DocumentResult(BaseModel):
    """The output for a single document."""

    source: str = Field(..., description="Input file path.")
    format: str = Field(..., description="Input format: pdf or docx.")
    title: Optional[str] = Field(None, description="Document title if known.")
    pages: int = Field(..., description="Number of pages.")
    risk: RiskLevel = Field(
        ..., description="Document-level overall risk flag (LOW/MEDIUM/HIGH)."
    )
    clauses: list[ExtractedClause] = Field(
        default_factory=list, description="Extracted clauses (rules + optional LLM)."
    )
    engine: str = Field(
        ..., description="Extraction engine used: 'rules' or 'rules+llm'."
    )
    generated_at: date = Field(..., description="Extraction date (YYYY-MM-DD).")
    warnings: list[str] = Field(
        default_factory=list, description="Non-fatal processing warnings."
    )


class BatchResult(BaseModel):
    """The output for a batch of documents."""

    documents: list[DocumentResult] = Field(
        default_factory=list, description="Per-document results."
    )
