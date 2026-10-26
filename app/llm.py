"""LLM: перевод + форматирование в стиле СВЕЖЕСТИ (промпт v3.1).

Оптимизации:
- один aiohttp-сеанс на процесс (keep-alive, без TLS-handshake на каждый вызов)
- параллельный перевод шорт-листа (asyncio.gather, семафор)
- логирование usage (вход/выход/cached_tokens) — видно, работает ли prompt caching
- ретраи с backoff на 5xx/таймауты ДО фолбэка на запасную модель
- max_output_tokens + таймаут 90с
- экспериментальный батч-режим: TRANSLATE_MODE=batch — один вызов на весь шорт-лист
"""
import asyncio
import json
import os
import re
import uuid

import aiohttp

_PROMPT = os.environ.get("LLM_PROMPT_PATH", "/app/prompt.txt")
if not os.path.exists(_PROMPT):  # запуск вне контейнера (тесты/локально)
    _PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "prompt.txt")
SYSTEM = open(_PROMPT, encoding="utf-8").read()

BASE = os.environ.get("OPENCODE_BASE", "https://opencode.ai/inference/go/v1")
MODEL = os.environ.get("OPENCODE_MODEL", "muse-spark-1.3-contributor")
FALLBACK_MODEL = os.environ.get("OPENCODE_MODEL_FALLBACK", "glm-5.3-flash")
SESSION = f"sess_{uuid.uuid4()}"
TIMEOUT = int(os.getenv("LLM_TIMEOUT", "90"))
MAX_OUT = int(os.getenv("LLM_MAX_OUTPUT", "2000"))
RETRIES = int(os.getenv("LLM_RETRIES", "2"))
MODE = os.getenv("TRANSLATE_MODE", "single")  # single | batch (экспериментально)
PARALLEL = int(os.getenv("LLM_PARALLEL", "5"))

USAGE = {"calls": 0, "in": 0, "out": 0, "cached": 0}

_session: aiohttp.ClientSession | None = None


def _sess() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT))
    return _session


def _headers() -> dict:
    key = os.environ.get("OPENCODE_API_KEY")
    if not key:
        raise SystemExit("OPENCODE_API_KEY пуст")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.0.0",
        "x-opencode-session": SESSION,
    }


def _log_usage(usage: dict, model: str):
    if not usage:
        return
    USAGE["calls"] += 1
    USAGE["in"] += usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    USAGE["out"] += usage.get("output_tokens") or usage.get("completion_tokens") or 0
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") \
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    USAGE["cached"] += cached
    print(f"[llm] {model}: in={usage.get('input_tokens') or usage.get('prompt_tokens')} "
          f"out={usage.get('output_tokens') or usage.get('completion_tokens')} "
          f"cached={cached} | всего: calls={USAGE['calls']} in={USAGE['in']} "
          f"out={USAGE['out']} cached={USAGE['cached']}")


def _clean(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            s = parts[1].strip()
    return s.strip().strip('" «» “”').strip()


def _extract_output(d: dict) -> str:
    txt = d.get("output_text") or ""
    if not txt:
        for item in d.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    txt += c.get("text", "")
    return _clean(txt)


async def _post_chat(model: str, prompt: str) -> str:
    payload = {"model": model, "max_completion_tokens": MAX_OUT, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]}
    async with _sess().post(BASE + "/chat/completions", json=payload, headers=_headers()) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError(f"chat {r.status}: {str(body)[:150]}")
        _log_usage(body.get("usage", {}), model)
        return _clean(body["choices"][0]["message"]["content"])


async def _post(model: str, prompt: str) -> str:
    """Вызов /responses с ретраями (backoff) на 5xx/сетевые сбои; 400 — навсегда."""
    payload = {
        "model": model,
        "max_output_tokens": MAX_OUT,
        "reasoning": {"effort": "low"},
        "input": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    no_reasoning = False
    for attempt in range(RETRIES + 1):
        try:
            async with _sess().post(BASE + "/responses", json=payload, headers=_headers()) as r:
                text = await r.text()
                if r.status == 200:
                    _log_usage(json.loads(text).get("usage", {}), model)
                    return _extract_output(json.loads(text))
                if r.status == 400 and "ModelProtocolUnsupported" in text:
                    return await _post_chat(model, prompt)
                if r.status == 400 and not no_reasoning:
                    no_reasoning = True
                    payload.pop("reasoning", None)
                    continue  # 400 с reasoning — ретраем без него один раз, без паузы
                raise RuntimeError(f"HTTP {r.status}: {text[:150]}")
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt >= RETRIES:
                raise
            wait = 2 * (2 ** attempt)
            print(f"[llm] {model} попытка {attempt + 1}: {type(e).__name__} — спим {wait}с")
            await asyncio.sleep(wait)
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 5" in msg and attempt < RETRIES:
                wait = 2 * (2 ** attempt)
                print(f"[llm] {model} попытка {attempt + 1}: {msg[:80]} — спим {wait}с")
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("retries exhausted")


async def translate(text: str) -> str | None:
    """Один пост. SKIP -> None; ошибка обоих провайдеров -> исключение."""
    prompt = "SOURCE MESSAGE:\n" + text + "\n\nOUTPUT:"
    last_exc = None
    for model in (MODEL, FALLBACK_MODEL):
        try:
            res = await _post(model, prompt)
            if res:
                return None if res.upper().startswith("SKIP") else res
        except Exception as e:
            last_exc = e
            print(f"[llm] {model}: {e} — fallback")
    if last_exc:
        raise last_exc
    return None


def _batch_prompt(texts: list[str]) -> str:
    user = "\n\n".join(f"===SOURCE {i + 1}===\n{t}" for i, t in enumerate(texts))
    fmt = (
        "\n\nЗАДАЧА: переведи и оформи КАЖДЫЙ источник выше по всем правилам канала. "
        "Если источник — реклама, не-новость или вне тем канала — выведи для него только SKIP.\n"
        "ВЫВОД СТРОГО В ФОРМАТЕ (каждый блок с новой строки, маркеры обязательны, ничего лишнего):\n"
        "===1===\n<перевод источника 1 или SKIP>\n"
        "===2===\n<перевод источника 2 или SKIP>\n"
        f"(всего источников: {len(texts)} — секций должно быть ровно столько)"
    )
    return user + fmt


def _parse_batch(out: str, n: int) -> list:
    """Разбор ответа батча: список из n элементов (str=перевод, None=SKIP, False=нет секции)."""
    res: list = [False] * n
    sections = re.split(r"===\s*(\d+)\s*===", out)
    # re.split: ['', '1', body1, '2', body2, ...]
    for i in range(1, len(sections) - 1, 2):
        idx = int(sections[i]) - 1
        if 0 <= idx < n:
            body = _clean(sections[i + 1])
            res[idx] = None if body.upper().startswith("SKIP") else body
    return res


async def translate_many(texts: list[str]) -> list:
    """Список -> список того же размера: str=перевод, None=SKIP, False=сбой (пост остаётся в бэклоге).

    MODE=batch: один вызов на весь список (экспериментально).
    MODE=single: параллельно, до LLM_PARALLEL одновременно."""
    if not texts:
        return []
    if MODE == "batch":
        for model in (MODEL, FALLBACK_MODEL):
            try:
                out = await _post(model, _batch_prompt(texts))
                res = _parse_batch(out, len(texts))
                if any(r is not False for r in res):
                    return res
            except Exception as e:
                print(f"[llm batch] {model}: {e} — fallback")
        return [False] * len(texts)
    sem = asyncio.Semaphore(PARALLEL)

    async def one(t):
        async with sem:
            try:
                out = await translate(t)
                return out if out is not None else None
            except Exception as e:
                print(f"[llm] перевод не удался: {str(e)[:100]}")
                return False

    return list(await asyncio.gather(*(one(t) for t in texts)))
