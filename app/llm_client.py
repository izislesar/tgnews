"""Клиент LLM на openai SDK.

Особенности провайдера (проверено спайком 22.11.2026):
- обязателен заголовок `x-opencode-session`, иначе 400 «cannot be routed efficiently»;
- `reasoning` по умолчанию съедает ВЕСЬ `max_output_tokens` и возвращает `status=incomplete`
  с пустым текстом — поэтому всегда low + запас токенов + явная обработка incomplete;
- структурный вывод работает через `text.format = json_schema`;
- `usage.cost` приходит в ответе — используем для учёта бюджета.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from .settings import Settings

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LlmError(RuntimeError):
    """Провайдер не смог ответить."""


class LlmIncomplete(LlmError):
    """Ответ оборван (обычно reasoning съел бюджет токенов)."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost += other.cost


class Prompts:
    """Файлы промптов; версия попадает в БД, чтобы вердикт был воспроизводим."""

    def __init__(self, directory: str | Path | None = None):
        self.dir = Path(directory) if directory else Path(__file__).parent / "prompts"
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._cache:
            path = self.dir / f"{name}.md"
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def version(self) -> str:
        import hashlib

        h = hashlib.sha1()
        for path in sorted(self.dir.glob("*.md")):
            h.update(path.name.encode())
            h.update(path.read_bytes())
        return h.hexdigest()[:12]


def loads_json(text: str) -> dict:
    """Достаёт JSON из ответа, даже если модель добавила ``` или прозу вокруг."""
    cleaned = _FENCE.sub("", text or "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise ValueError(f"в ответе нет JSON: {cleaned[:120]!r}")


class LlmClient:
    def __init__(self, settings: Settings, *, reasoning: str = "low"):
        if not settings.llm_key:
            raise SystemExit("OPENCODE_API_KEY пуст")
        self.settings = settings
        self.reasoning = reasoning
        self.max_output = settings.llm_max_output
        self.usage = Usage()
        self._client = AsyncOpenAI(
            base_url=settings.llm_base,
            api_key=settings.llm_key,
            timeout=settings.llm_timeout,
            max_retries=0,  # ретраи контролируем сами, иначе они умножаются
            default_headers={"x-opencode-session": f"sess_{uuid.uuid4().hex}"},
        )

    async def close(self) -> None:
        await self._client.close()

    # ---------- публичное ----------
    async def json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        name: str = "result",
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict:
        budget = max_tokens or self.max_output
        last: Exception | None = None
        for candidate in (model or self.settings.model, self.settings.fallback_model):
            for _ in range(2):  # второй заход — с увеличенным бюджетом токенов
                try:
                    return await self._one_json(candidate, system, user, schema, name, budget)
                except LlmIncomplete as exc:
                    last = exc
                    budget = min(int(budget * 1.8) + 500, 12_000)
                    continue
                except Exception as exc:  # 400/сеть — сразу к запасной модели
                    last = exc
                    break
        raise LlmError(f"JSON-ответ не получен: {last}") from last

    async def text(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> str:
        budget = max_tokens or self.max_output
        last: Exception | None = None
        for candidate in (model or self.settings.model, self.settings.fallback_model):
            for _ in range(2):
                try:
                    return await self._one_text(candidate, system, user, budget)
                except LlmIncomplete as exc:
                    last = exc
                    budget = min(int(budget * 1.8) + 500, 12_000)
                    continue
                except Exception as exc:
                    last = exc
                    break
        raise LlmError(f"текстовый ответ не получен: {last}") from last

    # ---------- внутреннее ----------
    @staticmethod
    def _input(system: str, user: str) -> list[dict]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _track(self, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        spec = Usage(calls=1)
        spec.input_tokens = getattr(usage, "input_tokens", 0) or 0
        spec.output_tokens = getattr(usage, "output_tokens", 0) or 0
        spec.cost = float(getattr(usage, "cost", 0) or 0)
        self.usage.add(spec)

    @staticmethod
    def _is_protocol_error(exc: Exception) -> bool:
        text = str(exc)
        return "ModelProtocolUnsupported" in text or "json_schema" in text

    async def _responses(self, model: str, payload: dict):
        return await self._client.responses.create(
            model=model, timeout=self.settings.llm_timeout, **payload
        )

    async def _one_json(
        self, model: str, system: str, user: str, schema: dict, name: str, budget: int
    ) -> dict:
        payload = dict(
            max_output_tokens=budget,
            reasoning={"effort": self.reasoning},
            input=self._input(system, user),
            text={"format": {"type": "json_schema", "name": name, "schema": schema,
                             "strict": True}},
        )
        try:
            response = await self._responses(model, payload)
        except Exception as exc:
            if self._is_protocol_error(exc):
                return await self._chat_json(model, system, user, budget)
            raise
        self._track(response)
        if getattr(response, "status", None) != "completed":
            raise LlmIncomplete(f"{model}: status={getattr(response, 'status', '?')}")
        return loads_json(getattr(response, "output_text", "") or "")

    async def _one_text(self, model: str, system: str, user: str, budget: int) -> str:
        payload = dict(
            max_output_tokens=budget,
            reasoning={"effort": self.reasoning},
            input=self._input(system, user),
        )
        response = await self._responses(model, payload)
        self._track(response)
        if getattr(response, "status", None) != "completed":
            raise LlmIncomplete(f"{model}: status={getattr(response, 'status', '?')}")
        text = (getattr(response, "output_text", "") or "").strip()
        if not text:
            raise LlmIncomplete(f"{model}: пустой текст")
        return text

    async def _chat_json(self, model: str, system: str, user: str, budget: int) -> dict:
        """Фолбэк для провайдеров без json_schema в /responses."""
        response = await self._client.chat.completions.create(
            model=model,
            messages=self._input(system, user),
            max_completion_tokens=budget,
            response_format={"type": "json_object"},
            timeout=self.settings.llm_timeout,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            spec = Usage(calls=1)
            spec.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            spec.output_tokens = getattr(usage, "completion_tokens", 0) or 0
            self.usage.add(spec)
        content = response.choices[0].message.content or ""
        return loads_json(content)

    async def health(self) -> dict:
        """Дешёвая проверка, что провайдер жив и JSON-схемы принимаются."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        }
        data = await self.json(
            system="Отвечай только JSON.",
            user="Верни {\"ok\": true}.",
            schema=schema,
            name="health",
            max_tokens=1200,
        )
        return {"ok": bool(data.get("ok")), "usage": self.usage}
