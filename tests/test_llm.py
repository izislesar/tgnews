"""Поведение клиента LLM на граничных ответах (без сети)."""

import asyncio

import pytest

from app import llm


def test_empty_output_is_error_not_skip(monkeypatch):
    async def fake_post(model, prompt):
        return ""

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.translate("текст поста"))


def test_explicit_skip_returns_none(monkeypatch):
    async def fake_post(model, prompt):
        return "SKIP"

    monkeypatch.setattr(llm, "_post", fake_post)
    assert asyncio.run(llm.translate("реклама")) is None


def test_fallback_used_when_first_model_empty(monkeypatch):
    seen = []

    async def fake_post(model, prompt):
        seen.append(model)
        return "" if len(seen) == 1 else "🔥 **Готовый пост**"

    monkeypatch.setattr(llm, "_post", fake_post)
    assert asyncio.run(llm.translate("текст")) == "🔥 **Готовый пост**"
    assert len(seen) == 2
