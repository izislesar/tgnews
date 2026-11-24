"""Политика проверки: независимость источников, пороги, текст дисклеймера.

Дисклеймер собирается КОДОМ, не моделью: иначе он поплывёт по стилю и формулировкам.
"""

from __future__ import annotations

from .models import ClaimType, ClaimVerdict, Evidence, PostVerdict, Status

# Группы издателей: подтверждением считаются только РАЗНЫЕ группы.
# Одна и та же новость в ria.ru и tass.ru — это один источник, а не два.
PUBLISHER_GROUPS: dict[str, str] = {
    # мировые агентства
    "reuters.com": "agencies-west",
    "apnews.com": "agencies-west",
    "afp.com": "agencies-west",
    "bloomberg.com": "agencies-west",
    "dpa.com": "agencies-west",
    "efe.com": "agencies-west",
    "ansa.it": "agencies-west",
    "kyodonews.net": "agencies-west",
    # качественные западные СМИ
    "cnn.com": "media-west",
    "bbc.com": "media-west",
    "bbc.co.uk": "media-west",
    "theguardian.com": "media-west",
    "nytimes.com": "media-west",
    "washingtonpost.com": "media-west",
    "wsj.com": "media-west",
    "ft.com": "media-west",
    "economist.com": "media-west",
    "aljazeera.com": "media-west",
    "dw.com": "media-west",
    "euronews.com": "media-west",
    "france24.com": "media-west",
    "rfi.fr": "media-west",
    "lemonde.fr": "media-west",
    "lesechos.fr": "media-west",
    "spiegel.de": "media-west",
    "faz.net": "media-west",
    "welt.de": "media-west",
    "zeit.de": "media-west",
    "sueddeutsche.de": "media-west",
    "nzz.ch": "media-west",
    "derstandard.at": "media-west",
    "elpais.com": "media-west",
    "elmundo.es": "media-west",
    "lavanguardia.com": "media-west",
    "corriere.it": "media-west",
    "repubblica.it": "media-west",
    "nbcnews.com": "media-west",
    "cbsnews.com": "media-west",
    "abcnews.go.com": "media-west",
    "politico.com": "media-west",
    "axios.com": "media-west",
    "time.com": "media-west",
    "forbes.com": "media-west",
    "businessinsider.com": "media-west",
    "telegraph.co.uk": "media-west",
    "independent.co.uk": "media-west",
    "thetimes.com": "media-west",
    "scmp.com": "media-west",
    "straitstimes.com": "media-west",
    "jpost.com": "media-west",
    "haaretz.com": "media-west",
    "timesofisrael.com": "media-west",
    "arabnews.com": "media-west",
    "gulfnews.com": "media-west",
    "al-monitor.com": "media-west",
    "middleeasteye.net": "media-west",
    "folha.uol.com.br": "media-west",
    "oglobo.globo.com": "media-west",
    "swissinfo.ch": "media-west",
    "delfi.lt": "media-west",
    "srf.ch": "media-west",
    "npr.org": "media-west",
    "cbc.ca": "media-west",
    "abc.net.au": "media-west",
    "larazon.es": "media-west",
    "abcnews.com": "media-west",
    # региональные агентства и СМИ
    "aa.com.tr": "agencies-regional",
    "trt.net.tr": "tr-state",
    "caliber.az": "regional-media",
    "report.az": "regional-media",
    "media.az": "regional-media",
    "armiya.az": "regional-media",
    "trend.az": "regional-media",
    "news.am": "regional-media",
    "kavkaz-uzel.eu": "regional-media",
    "jam-news.net": "regional-media",
    "timesofindia.indiatimes.com": "media-asia",
    "thehindu.com": "media-asia",
    # российские государственные / провластные
    "ria.ru": "ru-state",
    "tass.ru": "ru-state",
    "rt.com": "ru-state",
    "1tv.ru": "ru-state",
    "vgtrk.com": "ru-state",
    "iz.ru": "ru-state",
    "rg.ru": "ru-state",
    "lenta.ru": "ru-state",
    # российские частные
    "interfax.ru": "ru-private",
    "kommersant.ru": "ru-private",
    "rbc.ru": "ru-private",
    "vedomosti.ru": "ru-private",
    "forbes.ru": "ru-private",
    "gazeta.ru": "ru-private",
    "mk.ru": "ru-private",
    "kp.ru": "ru-private",
    "thebell.io": "ru-private",
    # российские независимые
    "meduza.io": "ru-independent",
    "novayagazeta.eu": "ru-independent",
    "istories.media": "ru-independent",
    "proekt.media": "ru-independent",
    "tvrain.tv": "ru-independent",
    "echo.msk.ru": "ru-independent",
    "theins.ru": "ru-independent",
    "rosbalt.ru": "ru-private",
    "ura.news": "ru-private",
    "www1.ru": "ru-private",
    "3dnews.ru": "media-tech",
    "notebookcheck-ru.com": "media-tech",
    # украинские
    "ukrinform.ua": "ua-state",
    "kyivindependent.com": "ua-media",
    "pravda.com.ua": "ua-media",
    "unian.ua": "ua-media",
    "espreso.tv": "ua-media",
    "hromadske.ua": "ua-media",
    "suspline.media": "ua-media",
    "nv.ua": "ua-media",
    "lb.ua": "ua-media",
    "zn.ua": "ua-media",
    "eurointegration.com.ua": "ua-media",
    "glavcom.ua": "ua-media",
    "korrespondent.net": "ua-media",
    "focus.ua": "ua-media",
    "ua.news": "ua-media",
    "news.telegraf.com.ua": "ua-media",
    "mil.gov.ua": "ua-official",
    "president.gov.ua": "ua-official",
    "armyinform.com.ua": "ua-official",
    # беларусь / кавказ / центральная азия
    "nashaniva.com": "by-media",
    "zerkalo.io": "by-media",
    "oc-media.org": "ca-media",
    "caucasuswatch.de": "ca-media",
    "fergana.agency": "ca-media",
    # официальные / первоисточники
    "kremlin.ru": "ru-official",
    "mil.ru": "ru-official",
    "mid.ru": "ru-official",
    "mod.gov.ru": "ru-official",
    "gov.uk": "official",
    "whitehouse.gov": "official",
    "state.gov": "official",
    "defense.gov": "official",
    "centcom.mil": "official",
    "un.org": "official",
    "unhcr.org": "official",
    "unocha.org": "official",
    "reliefweb.int": "official",
    "icrc.org": "official",
    "who.int": "official",
    "nato.int": "official",
    "osce.org": "official",
    "europa.eu": "official",
    "ec.europa.eu": "official",
    "consilium.europa.eu": "official",
    "imf.org": "official",
    "worldbank.org": "official",
    "iaea.org": "official",
    "sec.gov": "official",
    "federalreserve.gov": "official",
    # аналитика по конфликту
    "understandingwar.org": "analysis",
    "iswresearch.org": "analysis",
    "oryxspioenkop.com": "analysis",
    "bellingcat.com": "analysis",
    "acleddata.com": "analysis",
    "sipri.org": "analysis",
    "iiss.org": "analysis",
    "rusi.org": "analysis",
    "crisisgroup.org": "analysis",
    "hrw.org": "analysis",
    "amnesty.org": "analysis",
    # китайские
    "news.cn": "cn-state",
    "xinhuanet.com": "cn-state",
    "globaltimes.cn": "cn-state",
    # статистика: первоисточники данных, не СМИ
    "worldbank.org": "stat-official",
    "imf.org": "stat-official",
    "oecd.org": "stat-official",
    "unstats.un.org": "stat-official",
    "ilostat.ilo.org": "stat-official",
    "rosstat.gov.ru": "stat-official",
    "gks.ru": "stat-official",
    "banxico.org.mx": "stat-official",
    "inegi.org.mx": "stat-official",
    "bls.gov": "stat-official",
    "census.gov": "stat-official",
    "ons.gov.uk": "stat-official",
    "destatis.de": "stat-official",
    "insee.fr": "stat-official",
    # агрегаторы статистики — не первоисточник
    "statista.com": "stat-aggregator",
    "ourworldindata.org": "stat-aggregator",
    # энциклопедии — только как фон, не подтверждение
    "wikipedia.org": "reference",
}

NON_CONFIRMING_GROUPS = {"reference", "other"}  # незнакомый домен не считается подтверждением

# Для ПЕРЕПИСЫВАНИЯ фактов берём только независимые СМИ: государственные и
# официальные источники исключены — заказчик просил независимые, не государственные.
INDEPENDENT_GROUPS = {
    "agencies-west", "agencies-regional", "media-west", "media-asia",
    "regional-media", "ru-private", "ru-independent", "ua-media",
    "by-media", "ca-media", "analysis", "media-tech",
}
STATE_OR_OFFICIAL_GROUPS = {
    "ru-state", "ua-state", "cn-state", "tr-state",
    "official", "ru-official", "ua-official", "reference", "other",
}

MIN_CONFIDENCE = 0.7
MIN_GROUPS = 2
# Вывод по «не подтверждено» делаем только если реально просмотрели достаточно
# разных доменов. Иначе это не вердикт, а «мы почти не смотрели».
MIN_COVERAGE = 5
# Одного сообщения агентства достаточно: это отраслевая норма и не плодит
# «не подтверждено» на каждом реальном событии.
SUFFICIENT_SINGLE_GROUPS = {"agencies-west", "agencies-regional"}
# Официальная статистика (World Bank, МВФ, Росстат...) — первоисточник для чисел и дат.
STAT_GROUPS = {"stat-official"}

# Чем важнее статус, тем выше приоритет при агрегации по посту.
_SEVERITY = {
    Status.SUPPORTED: 0,
    Status.PARTIAL: 1,
    Status.NOT_FOUND: 2,
    Status.CONTESTED: 3,
    Status.REFUTED: 4,
}

_DISCLAIMER = {
    Status.REFUTED: "🚫 Источники опровергают: {claims}",
    Status.CONTESTED: "⚠️ Источники расходятся: {claims}",
    Status.NOT_FOUND: "⚠️ По открытым источникам не подтверждено: {claims}",
    Status.PARTIAL: "⚠️ Подтверждено частично: {claims}",
}


def configure(*, min_coverage: int | None = None) -> None:
    global MIN_COVERAGE
    if min_coverage is not None:
        MIN_COVERAGE = max(0, int(min_coverage))


def publisher_group(domain: str) -> str:
    domain = (domain or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    best, group = "", "other"
    for known, name in PUBLISHER_GROUPS.items():
        if domain == known or domain.endswith("." + known):
            if len(known) > len(best):
                best, group = known, name
    return group


def independent_groups(evidence: list[Evidence]) -> set[str]:
    return {e.publisher_group for e in evidence if e.publisher_group not in NON_CONFIRMING_GROUPS}


def needed_groups(verdict: ClaimVerdict) -> int:
    """Сколько независимых групп нужно для подтверждения.

    Заявление/цитата — достаточно первоисточника. Сообщение агентства — тоже
    достаточно (отраслевая норма). Остальное — минимум две группы.
    """
    if verdict.claim.ctype in (ClaimType.ATTRIBUTED, ClaimType.QUOTE):
        return 1
    # число/дата из официальной статистики — это первый источник данных, его хватает
    if verdict.claim.ctype in (ClaimType.NUMERIC, ClaimType.DATE) \
            and stat_evidence_domains(verdict):
        return 1
    if independent_groups(verdict.evidence) & SUFFICIENT_SINGLE_GROUPS:
        return 1
    return MIN_GROUPS


def independent_evidence_groups(verdict: ClaimVerdict) -> set[str]:
    """Группы источников, которым разрешено исправлять факты (не государственные)."""
    return independent_groups(verdict.evidence) & INDEPENDENT_GROUPS


def independent_evidence_domains(verdict: ClaimVerdict) -> set[str]:
    """Разные независимые ИЗДАНИЯ. Две редакции внутри одной группы — тоже два источника."""
    return {evidence.domain for evidence in verdict.evidence
            if evidence.domain and evidence.publisher_group in INDEPENDENT_GROUPS}


def stat_evidence_domains(verdict: ClaimVerdict) -> set[str]:
    """Первоисточники статистики: World Bank, МВФ, Росстат и т.п."""
    return {evidence.domain for evidence in verdict.evidence
            if evidence.domain and evidence.publisher_group in STAT_GROUPS}


def correction_eligible(verdict: ClaimVerdict, *, min_confidence: float = 0.8) -> bool:
    """Можно ли править факт по этому утверждению.

    Смотрим на СЫРОЙ вердикт судьи: «supported, но мало источников» (понижённый
    до partial) — это не ошибка в тексте, править нечего.
    """
    if verdict.corrected or not verdict.claim.is_core:
        return False
    if verdict.claim.ctype is ClaimType.OPINION:
        return False
    if verdict.status not in (Status.REFUTED, Status.CONTESTED, Status.PARTIAL):
        return False
    if verdict.confidence < min_confidence:
        return False
    domains = independent_evidence_domains(verdict)
    stat_domains = stat_evidence_domains(verdict)
    if not domains and not stat_domains:
        return False
    if verdict.claim.ctype in (ClaimType.ATTRIBUTED, ClaimType.QUOTE):
        return bool(domains)
    # числа и даты можно исправлять по официальной статистике — это первоисточник
    if verdict.claim.ctype in (ClaimType.NUMERIC, ClaimType.DATE) and stat_domains:
        return True
    # остальное: агентство или два разных независимых издания
    if any(evidence.publisher_group in SUFFICIENT_SINGLE_GROUPS
           for evidence in verdict.evidence if evidence.domain in domains):
        return True
    return len(domains) >= 2


def eligible_corrections(verdict: PostVerdict, *, min_confidence: float = 0.8) -> list[ClaimVerdict]:
    return [item for item in verdict.claims
            if correction_eligible(item, min_confidence=min_confidence)]


def confirmation_ok(verdict: ClaimVerdict) -> bool:
    if verdict.status is not Status.SUPPORTED:
        return False
    if verdict.confidence < MIN_CONFIDENCE:
        return False
    groups = independent_groups(verdict.evidence)
    # цитату/заявление не может подтвердить статистическая таблица
    if verdict.claim.ctype in (ClaimType.ATTRIBUTED, ClaimType.QUOTE) \
            and groups and groups <= STAT_GROUPS:
        return False
    return len(groups) >= needed_groups(verdict)


def effective_status(verdict: ClaimVerdict) -> Status:
    """supported без достаточных оснований понижается до partial."""
    if verdict.status is Status.SUPPORTED and not confirmation_ok(verdict):
        return Status.PARTIAL
    return verdict.status


# Вес статуса для оценки «насколько новость корректна».
_STATUS_WEIGHT = {
    Status.SUPPORTED: 1.0,
    Status.PARTIAL: 0.5,
    Status.CONTESTED: 0.25,
    Status.NOT_FOUND: 0.0,
    Status.REFUTED: 0.0,
}


def correctness_score(verdict: PostVerdict) -> float | None:
    """Доля корректных core-фактов 0..1. None — считать не из чего.

    Учитываются только утверждения, по которым реально смотрели достаточно
    источников (иначе это не «неверно», а «не проверяли»). Исправленные факты
    считаются корректными — они уже приведены к источникам.
    """
    scores: list[float] = []
    for item in verdict.claims:
        if item.corrected:
            scores.append(1.0)
            continue
        if not item.claim.is_core or item.claim.ctype is ClaimType.OPINION:
            continue
        if item.status is Status.SKIPPED or item.error:
            continue
        # «не нашли» — это отсутствие вывода, а не нулевая корректность.
        # Иначе правдивая, но не покрытая прессой новость получала бы 0%.
        status = effective_status(item)
        if status is Status.NOT_FOUND:
            continue
        if status is Status.SKIPPED:
            continue
        if item.coverage < MIN_COVERAGE:
            continue
        scores.append(_STATUS_WEIGHT.get(status, 0.0))
    if not scores:
        return None
    return sum(scores) / len(scores)


def rank(status: Status) -> int:
    """Чем больше — тем хуже для поста (refuted > contested > not_found > partial)."""
    return _SEVERITY.get(status, 0)


def aggregate(verdicts: list[ClaimVerdict]) -> Status:
    core = [v for v in verdicts
            if v.claim.is_core and v.claim.ctype is not ClaimType.OPINION]
    if not core:
        return Status.SKIPPED
    worst = Status.SUPPORTED
    for verdict in core:
        if verdict.corrected:  # факт исправлен в тексте — метка не нужна
            continue
        status = effective_status(verdict)
        if _SEVERITY[status] > _SEVERITY[worst]:
            worst = status
    return worst


def _quote(text: str, limit: int = 80) -> str:
    clean = " ".join(text.replace("\n", " ").split())
    clean = clean.strip(" .,;:—-")
    if len(clean) > limit:
        clean = clean[: limit - 1].rsplit(" ", 1)[0] + "…"
    return f"«{clean}»"


def labeled_claims(verdict: PostVerdict) -> list[ClaimVerdict]:
    """Только core-утверждения и только те, что дают основание для метки."""
    out = []
    for item in verdict.claims:
        if not item.claim.is_core or item.claim.ctype is ClaimType.OPINION:
            continue
        if item.corrected or effective_status(item) is Status.SUPPORTED:
            continue
        if item.coverage < MIN_COVERAGE:
            continue  # источников слишком мало — метку не ставим
        out.append(item)
    return out


def render_disclaimer(verdict: PostVerdict,
                      claim_texts: dict[int, str] | None = None) -> str | None:
    """Текст метки или None. Ставится только по core-утверждениям."""
    claim_texts = claim_texts or {}
    if verdict.degraded:  # инфраструктура не дала проверить — метку не врём
        return None
    status = aggregate(verdict.claims) if verdict.claims else verdict.status
    if status in (Status.SUPPORTED, Status.SKIPPED):
        return None
    template = _DISCLAIMER.get(status)
    if not template:
        return None
    claims = labeled_claims(verdict)
    if not claims:
        return None
    parts = [_quote(claim_texts.get(item.claim.idx, item.claim.text)) for item in claims[:3]]
    return template.format(claims=", ".join(parts))


def render_evidence(verdict: PostVerdict, *, limit: int = 6) -> list[str]:
    """Строки «как проверяли» для карточки админа и CLI."""
    lines: list[str] = []
    seen: set[str] = set()
    for item in verdict.claims:
        marker = effective_status(item).value
        lines.append(f"[{marker}] {item.claim.text[:120]}")
        for evidence in item.evidence[:2]:
            if evidence.url in seen:
                continue
            seen.add(evidence.url)
            lines.append(f"    {evidence.publisher_group}: {evidence.url}")
            if len(lines) >= limit:
                return lines
    return lines
