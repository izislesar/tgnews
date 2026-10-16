import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid

SYSTEM = open("/app/prompt.txt", encoding="utf-8").read()

# opencode go подписка: REST /responses с заголовком x-opencode-session
BASE = os.environ.get("OPENCODE_BASE", "https://opencode.ai/inference/go/v1")
MODEL = os.environ.get("OPENCODE_MODEL", "muse-spark-1.3-contributor")
FALLBACK_MODEL = os.environ.get("OPENCODE_MODEL_FALLBACK", "glm-5.3-flash")
SESSION = f"sess_{uuid.uuid4()}"
TIMEOUT = 240

CAP_MEDIA = 1000   # лимит подписи в Telegram — 1024 симв; запас
CAP_TEXT = 3800    # лимит обычного текстового сообщения — 4096


def _clean(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            s = parts[1].strip()
    s = s.strip().strip('" «» “”').strip()
    return s


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
        raise SystemExit("OPENCODE_API_KEY пуст — вписать oc_sk_... в .env")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "opencode/1.0.0",
        "x-opencode-session": SESSION,
    }


def _post_chat(model: str, prompt: str) -> str:
    """openai-completions (glm-5.3-flash и пр.)."""
    payload = {"model": model, "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]}
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers=_headers())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.load(r)
    return _clean(d["choices"][0]["message"]["content"])


def _post(model: str, prompt: str) -> str:
    """openai-responses (/responses), автопадение на /chat/completions."""
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
            print(f"[llm] {model}: /responses не поддержан, чат-эндпоинт")
            return _post_chat(model, prompt)
        if e.code == 400:
            payload.pop("reasoning", None)
            req = urllib.request.Request(BASE + "/responses",
                                         data=json.dumps(payload).encode(), headers=_headers())
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return _extract_output(json.load(r))
        print(f"[llm] HTTP {e.code} {model}: {body[:200]}")
        raise


def volume_plan(text: str, has_media: bool):
    """Правило объёма по СЛОВАМ:
      - источник <= 100 слов -> выводим примерно столько же, как в оригинале
      - источник длиннее -> сжатие до ~200 слов (или ~150, если есть медиа, чтобы
        влезть в лимит 1024 симв подписи, ≈150 слов)
    Возвращает (note, char_cap)."""
    words = len(text.split())
    cap = CAP_MEDIA if has_media else CAP_TEXT
    if words <= 100:
        note = (f"Слов в источнике: {words}. Перепиши примерно тем же объёмом "
                f"(около {words} слов). НЕ сокращай и не растягивай.")
    else:
        target = 150 if has_media else 200
        note = (f"Слов в источнике: {words}. Сожми примерно до {target} слов "
                f"(допуск ±15%). Помни про лимит: не превышай {cap} символов.")
    return note, cap


async def rewrite(text: str, note: str, char_cap: int) -> str | None:
    prompt = f"{note}\n\nSOURCE MESSAGE:\n{text}\n\nOUTPUT:"
    res = ""
    for model in (MODEL, FALLBACK_MODEL):
        try:
            res = await asyncio.to_thread(_post, model, prompt)
            if res:
                break
        except Exception as e:
            print(f"[llm] {model}: {e} — пробую fallback")
    if not res:
        return None
    if res.upper().startswith("SKIP"):
        return None
    if len(res.split()) < 8:  # огрызок
        return None
    # мягкий ретрай, если вылезло за кап (модели любят превышать)
    if len(res) > char_cap:
        hard = prompt.replace(
            "OUTPUT:",
            f"OUTPUT (HARD LIMIT: результат должен быть не длиннее {char_cap} символов; сократи):")
        for model in (MODEL, FALLBACK_MODEL):
            try:
                res2 = await asyncio.to_thread(_post, model, hard)
                if (res2
                        and not res2.upper().startswith("SKIP")
                        and len(res2.split()) >= 8
                        and len(res2) <= char_cap):
                    return res2
            except Exception as e:
                print(f"[llm retry] {model}: {e}")
        return None
    return res
