"""Shared exception types."""

from __future__ import annotations


class ContractExtractError(Exception):
    """Base error."""


class UnsupportedFormatError(ContractExtractError):
    """Input format is unsupported (not pdf/docx or corrupt)."""


class OfflineModeError(ContractExtractError):
    """LLM requested while in offline mode (LLM_OFFLINE=1)."""
