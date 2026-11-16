"""Optional Anthropic enhancement layer (mirrors OpenAI provider)."""

from __future__ import annotations

import json
import os

from contract_extract.llm.provider import LLMProvider
from contract_extract.schema import ExtractedClause, RiskLevel


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def active(self) -> bool:
        offline = os.environ.get("LLM_OFFLINE", "0") in ("1", "true", "yes", "on")
        return bool(self.api_key) and not offline

    def enhance(self, doc, clauses) -> list[ExtractedClause] | None:
        if not self.active():
            return None
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.api_key)
            prompt = self._build_prompt(doc, clauses)
            resp = client.messages.create(
                model=self.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text
            return self._parse(json.loads(raw), clauses)
        except Exception:
            return None

    def _build_prompt(self, doc, clauses) -> str:
        snippet = "\n".join(p.text[:900] for p in doc.pages[:3])
        known = [c.clause for c in clauses]
        return (
            "Return strict JSON with key clauses: [ {clause, page, quote, risk, flag} ]. "
            "Only include clauses NOT already in known_clauses. "
            f"known_clauses={known}\nDOCUMENT:\n{snippet}"
        )

    def _parse(self, data: dict, existing) -> list[ExtractedClause]:
        out: list[ExtractedClause] = []
        for item in data.get("clauses", []):
            try:
                c = ExtractedClause(
                    clause=str(item.get("clause", "")),
                    page=int(item.get("page", 1) or 1),
                    quote=str(item.get("quote", "")),
                    risk=RiskLevel(str(item.get("risk", "LOW")).upper()),
                    flag=item.get("flag"),
                )
                if c.clause and len(c.quote) > 5:
                    out.append(c)
            except Exception:
                continue
        return out
