"""Английский вход пайплайна и русская локализация результата для UI."""

from __future__ import annotations

import json
from typing import Any

from ..llm_client import LlmClient, Prompts
from .models import PostVerdict

LOCALIZE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "corrections"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idx", "text", "note"],
                "properties": {
                    "idx": {"type": "integer"},
                    "text": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idx", "old", "new"],
                "properties": {
                    "idx": {"type": "integer"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
            },
        },
    },
}


class EnglishTranslator:
    """Приводит пост к рабочему языку пайплайна (английский) до фактчекинга."""

    def __init__(self, llm: LlmClient, prompts: Prompts):
        self.llm = llm
        self.prompts = prompts

    async def to_english(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) < 20:
            return text
        try:
            translated = await self.llm.text(
                system=self.prompts.get("en"),
                user=text[:8000],
                max_tokens=3000,
            )
            return translated.strip() or text
        except Exception as exc:
            print(f"[fc.en] перевод не удался, работаем с оригиналом: {str(exc)[:120]}")
            return text


class FactcheckLocalizer:
    """Переводит утверждения и пояснения на русский для виджета и /fc."""

    def __init__(self, llm: LlmClient, prompts: Prompts, *, model: str | None = None):
        self.llm = llm
        self.prompts = prompts
        self.model = model

    async def localize(self, verdict: PostVerdict) -> dict[str, Any] | None:
        claims = [{"idx": item.claim.idx, "text": item.claim.text, "note": item.note}
                  for item in verdict.claims]
        corrections = [{"idx": item.claim_idx, "old": item.old, "new": item.new}
                       for item in verdict.corrections]
        if not claims:
            return None
        try:
            return await self.llm.json(
                system=self.prompts.get("localize"),
                user=json.dumps({"claims": claims, "corrections": corrections},
                                ensure_ascii=False),
                schema=LOCALIZE_SCHEMA,
                name="localized",
                model=self.model,
                max_tokens=2500,
            )
        except Exception as exc:
            print(f"[fc.loc] локализация не удалась: {str(exc)[:120]}")
            return None
