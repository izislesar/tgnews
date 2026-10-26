"""LLM: перевод + форматирование в стиле СВЕЖЕСТИ (промпт v3). Без лимитов объёма."""
import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid

SYSTEM = open("/app/prompt.txt", encoding="utf-8").read()

BASE = os.environ.get("OPENCODE_BASE", "https://opencode.ai/inference/go/v1")
MODEL = os.environ.get("OPENCODE_MODEL", "muse-spark-1.3-contributor")
FALLBACK_MODEL = os.environ.get("OPENCODE_MODEL_FALLBACK", "glm-5.3-flash")
SESSION = f"sess_{uuid.uuid4()}"
TIMEOUT = 240


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


def _post_chat(model: str, prompt: str) -> str:
    payload = {"model": model, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]}
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers=_headers())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return _clean(json.load(r)["choices"][0]["message"]["content"])


def _post(model: str, prompt: str) -> str:
    payload = {
        "model": model,
        "reasoning": {"effort": "low"},
        "input": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        req = urllib.request.Request(BASE + "/responses",
                                     data=json.dumps(payload).encode(), headers=_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return _extract_output(json.load(r))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 400 and "ModelProtocolUnsupported" in body:
            return _post_chat(model, prompt)
        if e.code == 400:
            payload.pop("reasoning", None)
            req = urllib.request.Request(BASE + "/responses",
                                         data=json.dumps(payload).encode(), headers=_headers())
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return _extract_output(json.load(r))
        print(f"[llm] HTTP {e.code} {model}: {body[:200]}")
        raise


async def translate(text: str) -> str | None:
    """Перевод + форматирование. SKIP/ошибка -> None."""
    prompt = "SOURCE MESSAGE:\n" + text + "\n\nOUTPUT:"
    res = ""
    for model in (MODEL, FALLBACK_MODEL):
        try:
            res = await asyncio.to_thread(_post, model, prompt)
            if res:
                break
        except Exception as e:
            print(f"[llm] {model}: {e} — fallback")
    if not res or res.upper().startswith("SKIP"):
        return None
    if res.upper().startswith("SKIP"):
        return None
    return res
