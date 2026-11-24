"""Виджет «результаты фактчекинга» под постом в выдаче бота.

Пост остаётся чистым и готовым к публикации; виджет — отдельным сообщением
сразу под ним: проценты, сколько источников, что не совпало, ссылки.
"""

from __future__ import annotations

import html
from urllib.parse import urlparse

from . import policy
from .models import ClaimType, PostVerdict, Status

_STATUS_ICON = {
    Status.SUPPORTED: "✅",
    Status.PARTIAL: "◐",
    Status.CONTESTED: "⚠️",
    Status.NOT_FOUND: "❔",
    Status.REFUTED: "🚫",
}
_STATUS_RU = {
    Status.SUPPORTED: "подтверждено",
    Status.PARTIAL: "частично",
    Status.CONTESTED: "источники расходятся",
    Status.NOT_FOUND: "не найдено",
    Status.REFUTED: "опровергнуто",
}


def independent_sources(verdict: PostVerdict, *, limit: int = 10) -> list[tuple[str, str]]:
    """Уникальные независимые издания: (домен, ссылка). Государственные не считаем."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for item in verdict.claims:
        for evidence in item.evidence:
            domain = evidence.domain
            if not domain or domain in seen:
                continue
            if evidence.publisher_group not in policy.INDEPENDENT_GROUPS:
                continue
            seen.add(domain)
            out.append((domain, evidence.url))
            if len(out) >= limit:
                return out
    return out


def statistical_sources(verdict: PostVerdict, *, limit: int = 5) -> list[tuple[str, str]]:
    """Первоисточники статистики: World Bank, МВФ, Росстат и т.п."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for item in verdict.claims:
        for evidence in item.evidence:
            domain = evidence.domain
            if not domain or domain in seen:
                continue
            if evidence.publisher_group not in policy.STAT_GROUPS:
                continue
            seen.add(domain)
            out.append((domain, evidence.url))
            if len(out) >= limit:
                return out
    return out


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_widget(verdict: PostVerdict, *, limit: int = 10,
                  localized: dict | None = None) -> str | None:
    """Текст виджета (HTML). None — показывать нечего."""
    if not verdict.claims:
        return None
    loc = localized or verdict.localized or {}
    loc_claims = {int(item.get("idx", -1)): item for item in loc.get("claims", [])}
    loc_corr = {int(item.get("idx", -1)): item for item in loc.get("corrections", [])}
    media = independent_sources(verdict, limit=limit)
    stat = statistical_sources(verdict)
    score = policy.correctness_score(verdict)

    if verdict.degraded:
        return "🔎 <b>Фактчекинг</b>: проверка не завершилась (поиск/загрузка недоступны)"

    reviewed = max((item.coverage for item in verdict.claims
                    if item.claim.is_core), default=0)
    header = "🔎 <b>Фактчекинг</b>"
    header += (": <b>%d%%</b>" % round(score * 100)) if score is not None \
        else ": недостаточно данных"
    pages = reviewed or (len(media) + len(stat))
    header += f" · страниц: {pages} · независимых: {len(media)}"
    if stat:
        header += f" · статистика: {len(stat)}"
    lines = [header]

    counts: dict[Status, int] = {}
    insufficient = 0
    for item in verdict.claims:
        if not item.claim.is_core or item.claim.ctype is ClaimType.OPINION:
            continue
        if not item.corrected and item.status is not Status.SKIPPED:
            if item.coverage < policy.MIN_COVERAGE:
                insufficient += 1
                continue
        key = Status.SUPPORTED if item.corrected else policy.effective_status(item)
        counts[key] = counts.get(key, 0) + 1
    if counts:
        parts = [f"{_STATUS_ICON[s]} {_STATUS_RU[s]} {counts[s]}"
                 for s in (Status.SUPPORTED, Status.PARTIAL, Status.CONTESTED,
                           Status.NOT_FOUND, Status.REFUTED) if counts.get(s)]
        lines.append(" · ".join(parts))

    mismatches = []
    for item in verdict.claims:
        if not item.claim.is_core or item.corrected:
            continue
        if item.coverage < policy.MIN_COVERAGE:
            continue  # источников мало — это не «не совпало»
        status = policy.effective_status(item)
        if status is Status.SUPPORTED:
            continue
        label = _STATUS_RU.get(status, status.value)
        text = loc_claims.get(item.claim.idx, {}).get("text") or item.claim.text
        note_src = loc_claims.get(item.claim.idx, {}).get("note") or item.note
        quote = " ".join(text.split())[:70]
        note = f" — {note_src[:90]}" if note_src else ""
        mismatches.append(f"• {quote} ({_esc(label)}){_esc(note)}")
    if mismatches:
        lines.append("<b>Что не совпало:</b>")
        lines.extend(mismatches[:3])

    if verdict.corrections:
        lines.append("<b>Исправлено:</b>")
        for item in verdict.corrections[:3]:
            loc_item = loc_corr.get(item.claim_idx, {})
            old = loc_item.get("old") or item.old
            new = loc_item.get("new") or item.new
            domain = urlparse(item.source_url).netloc
            lines.append(f"• {_esc(old[:70])} → {_esc(new[:70])}"
                         f" <i>({_esc(domain)})</i>")
    if insufficient:
        lines.append(f"ℹ️ по {insufficient} утверждени{'ю' if insufficient == 1 else 'ям'} "
                     f"источников меньше {policy.MIN_COVERAGE} — вывод не делался")
    links = [f'<a href="{html.escape(url)}">{_esc(domain)}</a>' for domain, url in media]
    links += [f'<a href="{html.escape(url)}">{_esc(domain)}</a> <i>(стат.)</i>'
              for domain, url in stat]
    if links:
        lines.append(f"<b>Источники:</b> {', '.join(links)}")
    return "\n".join(lines)
