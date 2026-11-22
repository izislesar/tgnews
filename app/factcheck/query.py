"""Построение и уточнение поисковых запросов."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Claim, ClaimVerdict, Query

_DENIAL = {
    "ru": "опроверг опровержение",
    "uk": "спростував",
    "en": "denies denied denied",
}
_STATEMENT = {
    "ru": "заявление официально",
    "uk": "заява офіційно",
    "en": "official statement says",
}
_CLEAN = re.compile(r'[«»"“”]')
_STOP = set("""
и в во не на с со что как а то же бы за по у из о от до для есть был была были было
его ее их этот эта это которые который которая которого при чем без под над про
the a an and or of in on at to for from with by is are was were be been has have
had will would could should that this these those his her their its it as not
""".split())
_WORD = re.compile(r"[\w\-]{4,}", re.UNICODE)


def _keywords(text: str, limit: int = 5) -> str:
    """Значимые слова: длинные имена/топонимы находятся лучше, чем цитата целиком."""
    words = [w for w in _WORD.findall(text) if w.lower() not in _STOP]
    return " ".join(words[:limit])


def _lang(claim: Claim) -> str:
    return (claim.lang or "ru").lower()[:2]


def _time_range(post_ts: float | None) -> str | None:
    if not post_ts:
        return None
    age_h = (datetime.now(timezone.utc).timestamp() - post_ts) / 3600
    if age_h <= 36:
        return "week"
    if age_h <= 24 * 14:
        return "month"
    return None


def _short(text: str, words: int = 10) -> str:
    return " ".join(_CLEAN.sub("", text).split()[:words])


def build_initial(claim: Claim, *, post_ts: float | None = None,
                  max_queries: int = 3) -> list[Query]:
    """1-3 запроса: сущности+числа, точная фраза, язык оригинала."""
    lang = _lang(claim)
    tr = _time_range(post_ts)
    queries: list[Query] = []

    anchor = " ".join([*claim.entities[:3], *claim.numbers[:2]]).strip()
    if not anchor:
        anchor = _keywords(claim.text) or _short(claim.text, 12)
    queries.append(Query(text=anchor, language=lang, time_range=tr))

    phrase = _keywords(claim.text, 8)
    if phrase and phrase.lower() not in anchor.lower():
        queries.append(Query(text=f'"{phrase}"', language=lang, time_range=tr))

    if claim.entities:
        queries.append(
            Query(text=" ".join(claim.entities[:3]) + " " + _STATEMENT.get(lang, ""),
                  language=lang, time_range=tr)
        )
    return _dedupe(queries)[:max_queries]


def refine(claim: Claim, verdict: ClaimVerdict, cycle: int,
           *, max_queries: int = 2) -> list[Query]:
    """Второй-третий цикл: точная цитата + запрос на опровержение."""
    lang = _lang(claim)
    out: list[Query] = []
    if claim.numbers:
        out.append(Query(text=" ".join([_short(claim.text, 8), *claim.numbers[:2]]),
                         language=lang, time_range="month"))
    out.append(Query(text=f'"{_short(claim.text, 7)}" ' + _DENIAL.get(lang, "denial"),
                     language=lang, time_range="year"))
    if verdict.note:
        out.append(Query(text=" ".join([*claim.entities[:2], _short(verdict.note, 5)]),
                         language=lang))
    return _dedupe(out)[:max_queries]


def _dedupe(queries: list[Query]) -> list[Query]:
    seen: set[str] = set()
    out: list[Query] = []
    for query in queries:
        text = " ".join(query.text.split())
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(query.model_copy(update={"text": text}))
    return out
