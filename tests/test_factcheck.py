"""Тесты фактчекинга: без сети и без LLM — только фейки портов."""

from __future__ import annotations

import asyncio

import pytest

from app.factcheck import policy
from app.factcheck.claims import ClaimExtractor, heuristic_claims
from app.factcheck.fetch import _is_private_ip, extract_document, select_snippets
from app.factcheck.judge import LlmJudge
from app.factcheck.models import (
    Claim,
    ClaimType,
    ClaimVerdict,
    Document,
    Evidence,
    Hit,
    PostVerdict,
    Priority,
    Snippet,
    Status,
)
from app.factcheck.query import build_initial, refine
from app.factcheck.service import FactCheckService
from app.settings import Settings


# ---------- фейки ----------
class FakePrompts:
    def get(self, name: str) -> str:
        return f"prompt:{name}"

    def version(self) -> str:
        return "test"


class FakeLlm:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload or {}
        self.error = error
        self.calls = 0

    async def json(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


class FakeSearch:
    def __init__(self, hits):
        # либо один список, либо список списков (по вызову на цикл)
        if hits and isinstance(hits[0], list):
            self.batches = list(hits)
        else:
            self.batches = [list(hits)]
        self.queries = []

    async def search(self, query):
        self.queries.append(query.text)
        batch = self.batches.pop(0) if len(self.batches) > 1 else self.batches[0]
        return list(batch)

    async def close(self):
        pass


class FakeFetch:
    def __init__(self, docs):
        self.docs = docs
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        return self.docs.get(url)

    async def close(self):
        pass


class FailingSearch:
    async def search(self, query):
        raise RuntimeError("SearXNG недоступен")

    async def close(self):
        pass


class FakeJudge:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0
        self.post_dates = []

    async def judge(self, claim, snippets, *, cycle=1, post_date=None):
        self.post_dates.append(post_date)
        self.calls += 1
        if self.verdicts:
            return self.verdicts.pop(0)
        return ClaimVerdict(claim=claim, status=Status.NOT_FOUND, cycles=cycle)


def make_settings(**overrides) -> Settings:
    base = dict(llm_key="test", fc_calls_per_post=6, fc_deadline_s=30.0,
                fc_judge_concurrency=2, fc_top_hits=2, fc_snippets_per_claim=2,
                fc_max_claims=4, fc_cycles=2)
    base.update(overrides)
    return Settings(**base)


def core_claim(text="Дроновые атаки почти вдвое снизили нефтепереработку в России",
               ctype=ClaimType.NUMERIC, numbers=("2026",)) -> Claim:
    return Claim(idx=0, text=text, ctype=ctype, priority=Priority.CORE,
                 numbers=list(numbers), entities=["Россия"])


def evidence_for(domain: str) -> Evidence:
    return Evidence(url=f"https://{domain}/a", domain=domain,
                    publisher_group=policy.publisher_group(domain))


# ---------- claims ----------
def test_claim_key_stable_across_punctuation_and_order():
    a = Claim(idx=0, text="Удар БПЛА по НПЗ в Рязани!", ctype=ClaimType.EVENT)
    b = Claim(idx=7, text="удар бпла по нпз в рязани", ctype=ClaimType.EVENT)
    c = Claim(idx=0, text="Удар БПЛА по НПЗ в Рязани!", ctype=ClaimType.NUMERIC)
    assert a.claim_key == b.claim_key
    assert a.claim_key != c.claim_key


def test_heuristic_claims_marks_numbers_as_core():
    claims = heuristic_claims(
        "Ночью дроны атаковали нефтеперерабатывающий завод в Рязани. "
        "Погибли 29 человек, ещё 12 пострадали. Власти обещают помощь."
    )
    assert claims, "эвристика должна что-то найти"
    assert any(c.is_core and c.ctype is ClaimType.NUMERIC for c in claims)


def test_extractor_falls_back_to_heuristic_when_llm_fails():
    llm = FakeLlm(error=RuntimeError("down"))
    extractor = ClaimExtractor(llm, FakePrompts(), max_claims=3)
    claims = asyncio.run(extractor.extract("Погибли 29 человек при ударе по заводу в Рязани."))
    assert claims and claims[0].is_core


def test_extractor_parses_and_dedupes_llm_payload():
    payload = {"claims": [
        {"text": "Погибли 29 человек при ударе по заводу", "ctype": "numeric",
         "priority": "core", "entities": ["Рязань"], "numbers": ["29"], "lang": "ru"},
        {"text": "погибли 29 человек при ударе по заводу!", "ctype": "numeric",
         "priority": "core", "entities": [], "numbers": [], "lang": "ru"},
        {"text": "Это было позорное решение властей", "ctype": "opinion", "priority": "core",
         "entities": [], "numbers": [], "lang": "ru"},
    ]}
    extractor = ClaimExtractor(FakeLlm(payload), FakePrompts(), max_claims=5)
    claims = asyncio.run(extractor.extract("текст"))
    assert len(claims) == 2
    opinion = [c for c in claims if c.ctype is ClaimType.OPINION][0]
    assert opinion.is_core is False  # мнение всегда secondary, даже если модель ошиблась


# ---------- политика ----------
def test_supported_needs_two_independent_groups():
    claim = core_claim()
    one = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.95,
                       evidence=[evidence_for("bbc.com")])
    two = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.95,
                       evidence=[evidence_for("bbc.com"), evidence_for("meduza.io")])
    assert policy.confirmation_ok(one) is False
    assert policy.effective_status(one) is Status.PARTIAL
    assert policy.confirmation_ok(two) is True
    assert policy.effective_status(two) is Status.SUPPORTED


def test_same_publisher_group_is_not_independent():
    claim = core_claim()
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                           evidence=[evidence_for("ria.ru"), evidence_for("tass.ru")])
    assert policy.confirmation_ok(verdict) is False


def test_attributed_claim_needs_only_primary_source():
    claim = core_claim("Минобороны заявило об уничтожении 12 дронов",
                       ctype=ClaimType.ATTRIBUTED, numbers=("12",))
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.8,
                           evidence=[evidence_for("mil.ru")])
    assert policy.confirmation_ok(verdict) is True


def test_aggregate_takes_worst_core_status():
    ok = ClaimVerdict(claim=core_claim("А"), status=Status.SUPPORTED, confidence=0.9,
                      evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")])
    bad = ClaimVerdict(claim=core_claim("Б"), status=Status.REFUTED, confidence=0.9)
    assert policy.aggregate([ok, bad]) is Status.REFUTED
    assert policy.aggregate([ok]) is Status.SUPPORTED
    assert policy.aggregate([]) is Status.SKIPPED


# ---------- дисклеймер ----------
def test_disclaimer_only_for_core_claims():
    core_bad = ClaimVerdict(claim=core_claim("Погибли 29 человек"),
                            status=Status.NOT_FOUND, coverage=5)
    secondary_bad = ClaimVerdict(
        claim=Claim(idx=1, text="подробности", ctype=ClaimType.EVENT,
                    priority=Priority.SECONDARY),
        status=Status.NOT_FOUND, coverage=5)
    verdict = PostVerdict(status=Status.NOT_FOUND, claims=[core_bad, secondary_bad])
    text = verdict.disclaimer
    assert text and "не подтверждено" in text.lower()
    assert "Погибли 29 человек" in text
    assert "подробности" not in text


def test_disclaimer_none_when_all_core_supported():
    claim = core_claim()
    verdict = PostVerdict(status=Status.SUPPORTED, claims=[
        ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                     evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")])
    ])
    assert verdict.disclaimer is None


def test_disclaimer_refuted_wording():
    verdict = PostVerdict(status=Status.REFUTED, claims=[
        ClaimVerdict(claim=core_claim("Погибли 300 человек"), status=Status.REFUTED,
                     confidence=0.95, coverage=5)
    ])
    assert "опровергают" in (verdict.disclaimer or "")


# ---------- судья ----------
def test_judge_drops_invented_evidence_indices():
    payload = {"status": "supported", "confidence": 0.9, "used": [7], "note": "ok"}
    judge = LlmJudge(FakeLlm(payload), FakePrompts())
    snippets = [Snippet(url="https://reuters.com/a", domain="reuters.com", quote="text")]
    verdict = asyncio.run(judge.judge(core_claim(), snippets))
    assert verdict.status is Status.NOT_FOUND  # без валидного evidence поддержка не считается
    assert verdict.evidence == []


def test_judge_attaches_evidence_and_group():
    payload = {"status": "supported", "confidence": 0.9, "used": [0], "note": "ok"}
    judge = LlmJudge(FakeLlm(payload), FakePrompts())
    snippets = [Snippet(url="https://reuters.com/a", domain="reuters.com", quote="text")]
    verdict = asyncio.run(judge.judge(core_claim(), snippets))
    assert verdict.status is Status.SUPPORTED
    assert verdict.evidence[0].publisher_group == "agencies-west"


def test_judge_low_confidence_refutation_becomes_contested():
    payload = {"status": "refuted", "confidence": 0.3, "used": [], "note": "?"}
    judge = LlmJudge(FakeLlm(payload), FakePrompts())
    snippets = [Snippet(url="https://reuters.com/a", domain="reuters.com", quote="text")]
    assert asyncio.run(judge.judge(core_claim(), snippets)).status is Status.CONTESTED


# ---------- fetch ----------
def test_private_ips_are_blocked():
    assert _is_private_ip("127.0.0.1")
    assert _is_private_ip("10.0.0.5")
    assert _is_private_ip("169.254.169.254")
    assert not _is_private_ip("8.8.8.8")


def test_extract_document_uses_paragraph_fallback():
    html = ("<html><head><title>T</title>"
            "<meta property='og:description' content='Краткое описание'>"
            "</head><body>" + "<p>Абзац с фактами и числами 29 человек.</p>" * 6 +
            "</body></html>").encode()
    doc = extract_document("https://example.com/a", html, min_text=200)
    assert doc and "29 человек" in doc.text


def test_select_snippets_prioritises_claim_terms():
    doc = Document(url="https://example.com/a", text=(
        "Погода сегодня хорошая. " * 5 +
        "Дроновые атаки почти вдвое снизили нефтепереработку в России в ноябре. "
        + "Спорт и культура. " * 5))
    snippets = select_snippets(doc, core_claim(), k=2)
    assert snippets and any("нефтепереработку" in s.quote for s in snippets)


# ---------- запросы ----------
def test_initial_queries_use_entities_and_numbers():
    queries = build_initial(core_claim())
    assert queries and "Россия" in queries[0].text
    assert len(queries) <= 3


def test_refine_adds_denial_query():
    claim = core_claim("Минобороны опровергло уничтожение 12 дронов",
                       ctype=ClaimType.ATTRIBUTED, numbers=("12",))
    verdict = ClaimVerdict(claim=claim, status=Status.NOT_FOUND, note="не нашли")
    queries = refine(claim, verdict, cycle=1)
    assert any("опроверг" in q.text for q in queries)


# ---------- сервис (циклы) ----------
def _service(settings, claim_payload, hits, docs, judge, *, corrector=None,
             fix_facts=False):
    extractor = ClaimExtractor(FakeLlm(claim_payload), FakePrompts(), max_claims=3)
    return FactCheckService(
        settings=settings,
        repo=None,
        search=FakeSearch(hits),
        fetcher=FakeFetch(docs),
        judge=judge,
        extractor=extractor,
        prompts=FakePrompts(),
        corrector=corrector,
        fix_facts=fix_facts,
    )


CLAIM_PAYLOAD = {"claims": [{
    "text": "Дроновые атаки почти вдвое снизили нефтепереработку в России",
    "ctype": "event", "priority": "core", "entities": ["Россия"],
    "numbers": [], "lang": "en"}]}
HITS = [Hit(url="https://www.reuters.com/a", title="R"), Hit(url="https://www.bbc.com/b", title="B")]
DOCS = {
    "https://www.reuters.com/a": Document(
        url="https://www.reuters.com/a",
        text="Drone attacks nearly halved Russia's November oil refining, sources said."),
    "https://www.bbc.com/b": Document(
        url="https://www.bbc.com/b",
        text="Drone strikes have nearly halved Russia's oil refining capacity this month."),
}


HITS2 = [Hit(url="https://apnews.com/c", title="AP"),
         Hit(url="https://www.theguardian.com/d", title="G")]
DOCS2 = {
    "https://apnews.com/c": Document(
        url="https://apnews.com/c",
        text="Drone attacks nearly halved Russia's refining capacity in November."),
    "https://www.theguardian.com/d": Document(
        url="https://www.theguardian.com/d",
        text="Ukrainian drones have nearly halved Russian oil refining this month."),
}


def test_service_stops_after_confirmation():
    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    judge = FakeJudge([ClaimVerdict(
        claim=claim, status=Status.SUPPORTED, confidence=0.92,
        evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")])])
    service = _service(make_settings(), CLAIM_PAYLOAD, HITS, DOCS, judge)
    verdict = asyncio.run(service.check_text("текст", tier="t2"))
    assert verdict.status is Status.SUPPORTED
    assert judge.calls == 1  # ранний выход, второй цикл не нужен
    assert verdict.disclaimer is None


def test_service_runs_second_cycle_when_not_found():
    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    judge = FakeJudge([
        ClaimVerdict(claim=claim, status=Status.NOT_FOUND, note="мало данных"),
        ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                     evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")]),
    ])
    service = _service(make_settings(), CLAIM_PAYLOAD, [HITS, HITS2], {**DOCS, **DOCS2}, judge)
    verdict = asyncio.run(service.check_text("текст", tier="t2"))
    assert judge.calls == 2
    assert verdict.status is Status.SUPPORTED


def test_service_degrades_on_budget():
    settings = make_settings(fc_calls_per_post=1)
    judge = FakeJudge([])
    service = _service(settings, CLAIM_PAYLOAD, HITS, DOCS, judge)
    verdict = asyncio.run(service.check_text("текст", tier="t1"))
    assert judge.calls == 0
    assert verdict.claims[0].error and "бюджет" in verdict.claims[0].error


def test_service_respects_deadline():
    settings = make_settings(fc_deadline_s=0.0)
    judge = FakeJudge([])
    service = _service(settings, CLAIM_PAYLOAD, HITS, DOCS, judge)
    verdict = asyncio.run(service.check_text("текст", tier="t2"))
    assert judge.calls == 0
    assert verdict.status is Status.NOT_FOUND


def test_service_passes_post_date_to_judge():
    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    judge = FakeJudge([ClaimVerdict(claim=claim, status=Status.NOT_FOUND)])
    service = _service(make_settings(), CLAIM_PAYLOAD, HITS, DOCS, judge)
    import time as _time
    asyncio.run(service.check_text("текст", tier="t1", post_ts=_time.time() - 3600))
    assert judge.post_dates and judge.post_dates[0]


def test_service_marks_degraded_when_search_is_down():
    settings = make_settings()
    extractor = ClaimExtractor(FakeLlm(CLAIM_PAYLOAD), FakePrompts(), max_claims=3)
    judge = FakeJudge([])
    service = FactCheckService(
        settings=settings, repo=None, search=FailingSearch(),
        fetcher=FakeFetch(DOCS), judge=judge, extractor=extractor, prompts=FakePrompts())
    verdict = asyncio.run(service.check_text("текст", tier="t1"))
    assert verdict.degraded is True
    assert judge.calls == 0
    assert verdict.disclaimer is None  # «не проверили» != «не подтверждено»
    assert "поиск недоступен" in (verdict.claims[0].error or "")


def test_service_skips_when_no_core_claims():
    payload = {"claims": [{"text": "Это была прекрасная речь", "ctype": "opinion",
                           "priority": "secondary", "entities": [], "numbers": [],
                           "lang": "ru"}]}
    service = _service(make_settings(), payload, HITS, DOCS, FakeJudge([]))
    verdict = asyncio.run(service.check_text("текст"))
    assert verdict.status is Status.SKIPPED
    assert verdict.disclaimer is None


# ---------- конфиг ----------
def test_settings_load_from_env_file(tmp_path, monkeypatch):
    for name in ("OPENCODE_MODEL", "FC_CYCLES", "SEARX_URL", "FC_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENCODE_MODEL=test-model\nFC_CYCLES=3\n"
                   "SEARX_URL=http://searx:8080\nFC_ENABLED=shadow\n", encoding="utf-8")
    settings = Settings.load(env)
    assert settings.model == "test-model"
    assert settings.fc_cycles == 3
    assert settings.searx_url == "http://searx:8080"
    assert settings.fc_mode == "shadow"


def test_single_agency_report_is_enough():
    claim = core_claim()
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                           evidence=[evidence_for("reuters.com")])
    assert policy.confirmation_ok(verdict) is True


def test_lead_paragraph_always_in_snippets():
    doc = Document(url="https://example.com/a", text=(
        "Минобороны сообщило о четырёхсторонней встрече в Тегеране. "
        "Подробности позже. " + "Погода и спорт. " * 10))
    snippets = select_snippets(doc, core_claim("Иран провёл встречу с Пакистаном"),
                               k=1)
    assert snippets and "Тегеране" in snippets[0].quote


def test_unknown_domains_do_not_confirm():
    claim = core_claim()
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.95,
                           evidence=[evidence_for("random-blog.example"),
                                     evidence_for("another.example")])
    assert policy.publisher_group("random-blog.example") == "other"
    assert policy.confirmation_ok(verdict) is False


# ---------- хранилище ----------
def test_repo_saves_and_loads_verdict(tmp_path):
    import sqlite3
    from app.factcheck.repo import FactcheckRepo

    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("CREATE TABLE posts(qid INTEGER PRIMARY KEY, text TEXT, ts REAL)")
    con.execute("INSERT INTO posts(qid, text, ts) VALUES(1, 'текст', 1000.0)")
    repo = FactcheckRepo(con)

    verdict = PostVerdict(status=Status.NOT_FOUND, qid=1, claims=[
        ClaimVerdict(claim=core_claim(), status=Status.NOT_FOUND,
                     evidence=[evidence_for("reuters.com")])])
    repo.save(1, verdict, version="v1")

    loaded = repo.load(1)
    assert loaded is not None
    assert loaded.status is Status.NOT_FOUND
    assert loaded.claims[0].evidence[0].domain == "reuters.com"
    assert loaded.claims[0].claim.claim_key == core_claim().claim_key

    repo.enqueue(1)
    assert repo.claim_next() == 1
    repo.fail(1, "сбой")
    assert repo.pending() == 1
    repo.finish(1)
    assert repo.pending() == 0


# ---------- клиент поиска ----------
def test_search_caches_repeated_queries():
    from app.factcheck.searxng import SearxngSearch
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080")
    calls = []

    async def fake_get(params):
        calls.append(dict(params))
        return {"results": [{"url": "https://www.reuters.com/a", "title": "t",
                             "content": "c", "engine": "bing", "score": 1.0}],
                "unresponsive_engines": []}

    client._get = fake_get
    first = asyncio.run(client.search(Query(text="x", language="en")))
    second = asyncio.run(client.search(Query(text="x", language="en")))
    assert len(first) == len(second) == 1
    assert len(calls) == 1  # второй запрос обслужен кэшем


def test_search_raises_when_all_engines_down():
    from app.factcheck.searxng import SearxngSearch, SearchError
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080", fallback_engines="bing,brave")

    async def fake_get(params):
        return {"results": [], "unresponsive_engines": [["google cse", "Suspended"]]}

    client._get = fake_get
    with pytest.raises(SearchError):
        asyncio.run(client.search(Query(text="x")))


def test_search_returns_empty_when_engines_are_alive():
    from app.factcheck.searxng import SearxngSearch
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080")

    async def fake_get(params):
        return {"results": [], "unresponsive_engines": []}

    client._get = fake_get
    assert asyncio.run(client.search(Query(text="x"))) == []


# ---------- интеграция в бота ----------
def _stub_bot(settings=None):
    from app.bot import AdminBot
    from app.store import Store

    settings = settings or Settings(bot_token="123456789:AA" + "x" * 33, fc_mode="on")
    store = Store(":memory:")
    return AdminBot(store, None, {1}, settings=settings), store


def test_body_keeps_post_clean_and_tags_low_correctness():
    bot, _store = _stub_bot()
    post = {"ru_text": "✅ **Текст поста**", "username": "ch", "ids": "10"}

    partial = PostVerdict(status=Status.PARTIAL, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.PARTIAL,
                     confidence=0.8, coverage=5)])
    clean = bot._body(post, partial)
    assert "не подтверждено" not in clean.lower()
    assert "Низкая корректность" not in clean

    bad = PostVerdict(status=Status.REFUTED, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.REFUTED,
                     confidence=0.9, coverage=5)])
    assert "Низкая корректность" in bot._body(post, bad)


def test_verdict_text_contains_evidence_and_status():
    bot, _store = _stub_bot()
    verdict = PostVerdict(qid=7, status=Status.SUPPORTED, claims=[
        ClaimVerdict(claim=core_claim(), status=Status.SUPPORTED, confidence=0.9,
                     evidence=[evidence_for("reuters.com")])])
    text = bot._verdict_text(verdict)
    assert "qid=7" in text and "подтверждено" in text
    assert "reuters.com" in text


def test_note_verdict_does_nothing_off_mode():
    bot, _store = _stub_bot(Settings(bot_token="123456789:AA" + "x" * 33, fc_mode="off"))
    verdict = PostVerdict(qid=1, status=Status.NOT_FOUND)
    asyncio.run(bot.note_verdict(1, verdict))  # не должно падать и ничего не шлёт


def test_store_qid_by_key_and_cards():
    from app.store import Store

    store = Store(":memory:")
    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "key-1", "текст", 10, 1, 0)
    qid = store.qid_by_key("key-1")
    assert qid
    store.map_card(100, 200, qid)
    assert store.cards_for_qid(qid) == [(100, 200, "text")]


def test_note_verdict_edits_card_and_sends_widget(monkeypatch):
    bot, store = _stub_bot()
    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "key-9", "текст", 10, 1, 0)
    qid = store.qid_by_key("key-9")
    store.set_ru_text(qid, "✅ **Пост**")
    store.map_card(100, 200, qid)

    edits, sent = [], []

    async def fake_edit(text, chat_id, message_id, parse_mode=None):
        edits.append((chat_id, message_id, text))

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return type("M", (), {"message_id": 1})()

    monkeypatch.setattr(bot.bot, "edit_message_text", fake_edit)
    monkeypatch.setattr(bot.bot, "send_message", fake_send)

    verdict = PostVerdict(qid=qid, status=Status.REFUTED, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.REFUTED,
                     confidence=0.9, coverage=6,
                     evidence=[evidence_for("reuters.com")])])
    asyncio.run(bot.note_verdict(qid, verdict))

    assert edits and "Низкая корректность" in edits[0][2]
    assert any("Фактчекинг" in text for _chat, text, _kw in sent)
    assert any("reuters.com" in text for _chat, text, _kw in sent)


def test_admin_ids_parsing():
    from app.main import _admin_ids

    assert _admin_ids("1, 2 ,3") == {1, 2, 3}
    assert _admin_ids("") == set()


# ---------- регрессии второго прохода ----------
def _repo_with_post(tmp_path):
    import sqlite3
    from app.factcheck.repo import FactcheckRepo

    con = sqlite3.connect(tmp_path / "r.db")
    con.execute("CREATE TABLE posts(qid INTEGER PRIMARY KEY, text TEXT, ts REAL)")
    con.execute("INSERT INTO posts(qid, text, ts) VALUES(1, 'текст', 1.0)")
    return con, FactcheckRepo(con)


def test_repo_load_is_none_before_check(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    assert repo.load(1) is None  # нет fc_status -> «не проверяли», а не «skipped»


def test_repo_persists_degraded(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    repo.save(1, PostVerdict(qid=1, status=Status.NOT_FOUND, degraded=True,
                             claims=[ClaimVerdict(claim=core_claim(), status=Status.NOT_FOUND)]))
    loaded = repo.load(1)
    assert loaded is not None and loaded.degraded is True
    assert loaded.disclaimer is None  # degraded не даёт метки и после перезагрузки


def test_repo_stops_retrying_after_cap(tmp_path):
    con, repo = _repo_with_post(tmp_path)
    repo.enqueue(1)
    for _ in range(3):
        assert repo.claim_next() == 1
        repo.fail(1, "boom")
        con.execute("UPDATE fc_jobs SET next_try=0")
        con.commit()
    assert repo.claim_next() is None  # исчерпала попытки -> error
    assert repo.pending() == 0


def test_invalid_fc_mode_falls_back_to_off(tmp_path, monkeypatch):
    for name in ("FC_ENABLED",):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("FC_ENABLED=true\n", encoding="utf-8")
    assert Settings.load(env).fc_mode == "off"


def test_note_verdict_ignores_undelivered(monkeypatch):
    bot, store = _stub_bot()
    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "key-undelivered", "текст", 1, 1, 0)
    qid = store.qid_by_key("key-undelivered")

    calls = []

    async def fake_edit(*args, **kwargs):
        calls.append("edit")

    async def fake_send(*args, **kwargs):
        calls.append("send")

    monkeypatch.setattr(bot.bot, "edit_message_text", fake_edit)
    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    verdict = PostVerdict(qid=qid, status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                     coverage=5)])
    asyncio.run(bot.note_verdict(qid, verdict))
    assert calls == []  # карточку админ ещё не получал — не шумим


def test_verdict_text_handles_empty_claims():
    bot, _ = _stub_bot()
    text = bot._verdict_text(PostVerdict(qid=5, status=Status.SKIPPED))
    assert "Утверждений для проверки нет" in text


def test_store_state_by_key():
    from app.store import Store

    store = Store(":memory:")
    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "k-new", "Удар по заводу в Рязани, 29 человек погибло", 10, 1, 0)
    store.upsert_post(1, "ch", "6", "k-dup", "Ночной удар по заводу в Рязани, 29 человек погибло", 10, 1, 0)
    assert store.state_by_key("k-new") == "new"
    assert store.state_by_key("k-dup") == "dup"


def test_pool_copy_gets_tag_reply_and_widget(monkeypatch):
    from app.bot import AdminBot
    from app.store import Store

    settings = Settings(bot_token="123456789:AA" + "x" * 33, fc_mode="on",
                        approval_chat_id="777")
    store = Store(":memory:")
    bot = AdminBot(store, None, {1}, settings=settings, fc_repo=None)

    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "key-pool", "текст", 1, 1, 0)
    qid = store.qid_by_key("key-pool")
    store.set_ru_text(qid, "✅ **Пост**")

    sent, edits = [], []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return type("M", (), {"message_id": 900})()

    async def fake_edit(text, chat_id, message_id, parse_mode=None):
        edits.append((chat_id, message_id, text))

    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    monkeypatch.setattr(bot.bot, "edit_message_text", fake_edit)

    user = type("U", (), {"username": "ed", "first_name": "Ed"})()
    assert asyncio.run(bot._send_to_pool(qid, user)) is True
    assert sent and sent[0][0] == 777
    assert sent[0][2].get("reply_markup") is None  # без кнопок модерации

    verdict = PostVerdict(qid=qid, status=Status.REFUTED, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.REFUTED,
                     confidence=0.9, coverage=6, evidence=[evidence_for("reuters.com")])])
    asyncio.run(bot.note_verdict(qid, verdict))

    pool_texts = [text for chat_id, text, _kw in sent if chat_id == 777]
    assert any("Низкая корректность" in text for text in pool_texts)
    assert any("Фактчекинг" in text for text in pool_texts)
    # «Добавил: …» не затёрт — копия не редактировалась
    assert not edits


def test_album_gets_single_label_in_pool(monkeypatch):
    from app.bot import AdminBot
    from app.store import Store

    settings = Settings(bot_token="123456789:AA" + "x" * 33, fc_mode="on",
                        approval_chat_id="777")
    store = Store(":memory:")
    bot = AdminBot(store, None, {1}, settings=settings, fc_repo=None)
    store.add_channel("ch", "t")
    store.upsert_post(1, "ch", "5", "key-album", "текст", 1, 1, 1)
    qid = store.qid_by_key("key-album")
    store.set_ru_text(qid, "✅ **Пост**")
    store.remember_fileids("key-album", '[{"kind": "photo", "id": "f1"},'
                                        ' {"kind": "video", "id": "f2"}]')

    sent = []
    counter = {"n": 0}

    def _msg():
        counter["n"] += 1
        return type("M", (), {"message_id": counter["n"]})()

    async def fake_media_group(chat_id, media):
        return [_msg(), _msg()]

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return _msg()

    async def fake_edit(*args, **kwargs):
        return None

    monkeypatch.setattr(bot.bot, "send_media_group", fake_media_group)
    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    monkeypatch.setattr(bot.bot, "edit_message_text", fake_edit)

    asyncio.run(bot._deliver(777, "key-album", "body", qid=qid, with_markup=False))
    kinds = sorted(kind for _c, _m, kind in store.cards_for_qid(qid))
    assert kinds == ["media", "media", "text", "widget"]

    verdict = PostVerdict(qid=qid, status=Status.REFUTED, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.REFUTED,
                     confidence=0.9, coverage=6,
                     evidence=[evidence_for("reuters.com")])])
    asyncio.run(bot.note_verdict(qid, verdict))
    widgets = [text for chat_id, text, _ in sent if chat_id == 777
               and "Фактчекинг" in text]
    tags = [text for chat_id, text, _ in sent if chat_id == 777
            and "Низкая корректность" in text]
    assert len(widgets) == 1, "виджет должен быть один, а не на каждое медиа"
    assert len(tags) == 1


def test_job_tier_is_preserved(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    repo.enqueue(1, "t2")
    assert repo.claim_next() == 1
    assert repo.job_tier(1) == "t2"


# ---------- вариант 2: переписывание под независимые источники ----------
def _refuted_verdict(domains=("reuters.com", "bbc.com"), conf=0.9,
                     ctype=ClaimType.NUMERIC):
    claim = core_claim("Погибли 29 человек", ctype=ctype, numbers=("29",))
    return ClaimVerdict(claim=claim, status=Status.REFUTED, confidence=conf,
                        evidence=[evidence_for(domain) for domain in domains])


def test_correction_blocked_for_state_media():
    verdict = _refuted_verdict(("ria.ru", "tass.ru"))
    assert policy.correction_eligible(verdict, min_confidence=0.8) is False


def test_correction_allows_single_independent_agency():
    assert policy.correction_eligible(_refuted_verdict(("reuters.com",)),
                                      min_confidence=0.8) is True


def test_correction_needs_two_media_without_agency():
    assert policy.correction_eligible(_refuted_verdict(("bbc.com",)),
                                      min_confidence=0.7) is False
    assert policy.correction_eligible(_refuted_verdict(("bbc.com", "meduza.io")),
                                      min_confidence=0.7) is True


def test_correction_counts_two_outlets_of_same_group():
    # BBC и Guardian — разные редакции, хотя группа одна
    assert policy.correction_eligible(_refuted_verdict(("bbc.com", "theguardian.com")),
                                      min_confidence=0.7) is True


def test_correction_ignores_weakly_supported_claim():
    verdict = ClaimVerdict(claim=core_claim(), status=Status.SUPPORTED,
                           confidence=0.9, evidence=[evidence_for("bbc.com")])
    assert policy.correction_eligible(verdict) is False


def test_corrector_applies_valid_change():
    from app.factcheck.correct import LlmCorrector

    payload = {
        "changed": True,
        "corrected_text": "Погибли 12 человек (по данным Reuters).",
        "confidence": 0.92,
        "corrections": [{"claim_idx": 0, "old": "29 человек", "new": "12 человек",
                         "source_url": "https://reuters.com/a"}],
    }
    corrector = LlmCorrector(FakeLlm(payload), FakePrompts())
    verdict = PostVerdict(claims=[_refuted_verdict(("reuters.com", "bbc.com"))])
    result = asyncio.run(corrector.correct("Погибли 29 человек.", verdict))
    assert result.changed is True
    assert result.text and "12 человек" in result.text
    assert result.fixed == {0}


def test_corrector_rejects_invented_source():
    from app.factcheck.correct import LlmCorrector

    payload = {
        "changed": True,
        "corrected_text": "Погибли 12 человек (по данным Reuters).",
        "confidence": 0.95,
        "corrections": [{"claim_idx": 0, "old": "29 человек", "new": "12 человек",
                         "source_url": "https://fake.example/x"}],
    }
    corrector = LlmCorrector(FakeLlm(payload), FakePrompts())
    verdict = PostVerdict(claims=[_refuted_verdict(("reuters.com",))])
    assert asyncio.run(corrector.correct("Погибли 29 человек.", verdict)).changed is False


def test_corrector_rejects_low_confidence():
    from app.factcheck.correct import LlmCorrector

    payload = {
        "changed": True,
        "corrected_text": "Погибли 12 человек (по данным Reuters).",
        "confidence": 0.3,
        "corrections": [{"claim_idx": 0, "old": "29 человек", "new": "12 человек",
                         "source_url": "https://reuters.com/a"}],
    }
    corrector = LlmCorrector(FakeLlm(payload), FakePrompts())
    verdict = PostVerdict(claims=[_refuted_verdict(("reuters.com",))])
    assert asyncio.run(corrector.correct("Погибли 29 человек.", verdict)).changed is False


def test_service_rewrites_post_and_drops_label():
    from app.factcheck.correct import LlmCorrector

    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    judge = FakeJudge([ClaimVerdict(
        claim=claim, status=Status.REFUTED, confidence=0.9,
        evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")])])
    correction_payload = {
        "changed": True,
        "corrected_text": ("Дроновые атаки снизили нефтепереработку в России примерно "
                           "на треть (по данным Reuters)."),
        "confidence": 0.9,
        "corrections": [{"claim_idx": 0, "old": "почти вдвое", "new": "примерно на треть",
                         "source_url": "https://reuters.com/a"}],
    }
    corrector = LlmCorrector(FakeLlm(correction_payload), FakePrompts())
    service = _service(make_settings(fc_fix_facts=True), CLAIM_PAYLOAD, HITS, DOCS,
                       judge, corrector=corrector, fix_facts=True)
    original = ("Дроновые атаки почти вдвое снизили нефтепереработку в России в ноябре, "
                "сообщают источники.")
    verdict = asyncio.run(service.check_text(original, tier="t2", fix_facts=True))
    assert verdict.corrected_text and "на треть" in verdict.corrected_text
    assert verdict.claims[0].corrected is True
    assert verdict.disclaimer is None  # исправленный факт метку не требует


def test_body_uses_corrected_text():
    bot, _store = _stub_bot()
    verdict = PostVerdict(qid=1, status=Status.SUPPORTED,
                          corrected_text="✅ **Исправленный текст**",
                          corrections=[])
    body = bot._body({"ru_text": "Старый текст", "username": "ch", "ids": "1"}, verdict)
    assert "Исправленный текст" in body and "Старый текст" not in body


def test_repo_persists_correction(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    from app.factcheck.models import CorrectionItem

    verdict = PostVerdict(qid=1, status=Status.SUPPORTED,
                          corrected_text="Исправленный текст",
                          corrections=[CorrectionItem(claim_idx=0, claim="x", old="29",
                                                      new="12",
                                                      source_url="https://reuters.com/a")],
                          claims=[ClaimVerdict(claim=core_claim(), status=Status.REFUTED,
                                               corrected=True)])
    repo.save(1, verdict, version="v1")
    loaded = repo.load(1)
    assert loaded is not None
    assert loaded.corrected_text == "Исправленный текст"
    assert loaded.corrections[0].new == "12"
    assert loaded.claims[0].corrected is True


def test_corrected_text_strips_service_markers():
    from app.factcheck.correct import LlmCorrector

    payload = {
        "changed": True,
        "corrected_text": "<<<\nПогибли 12 человек (по данным Reuters).\n>>>",
        "confidence": 0.9,
        "corrections": [{"claim_idx": 0, "old": "29 человек", "new": "12 человек",
                         "source_url": "https://reuters.com/a"}],
    }
    corrector = LlmCorrector(FakeLlm(payload), FakePrompts())
    verdict = PostVerdict(claims=[_refuted_verdict(("reuters.com",))])
    result = asyncio.run(corrector.correct("Погибли 29 человек.", verdict))
    assert result.text == "Погибли 12 человек (по данным Reuters)."


def test_search_empty_from_fallback_is_not_an_outage():
    from app.factcheck.searxng import SearxngSearch
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080", fallback_engines="bing")
    calls = []

    async def fake_get(params):
        calls.append(params)
        if params.get("engines") == client.engines and "," in params.get("engines", ""):
            return {"results": [], "unresponsive_engines": [["google cse", "Suspended"]]}
        return {"results": [], "unresponsive_engines": []}  # bing ответил, пусто

    client._get = fake_get
    assert asyncio.run(client.search(Query(text="x"))) == []  # не исключение
    assert len(calls) == 2


# ---------- production hardening ----------
def test_split_html_respects_limit_and_keeps_content():
    from app.bot import split_html

    text = "\n".join("Длинная строка с текстом поста и ссылкой " * 20 for _ in range(15))
    chunks = split_html(text, limit=4000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert all(chunk.strip() for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) >= len(text) * 0.9


def test_split_html_short_text_is_single_chunk():
    from app.bot import split_html

    assert split_html("короткий пост") == ["короткий пост"]


def test_send_text_falls_back_to_plain_text(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    bot, _store = _stub_bot()
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        if kwargs.get("parse_mode") == "HTML" and "<b>" in text:
            raise TelegramBadRequest(None, "Bad Request: can't parse entities: ...")
        sent.append(text)
        return type("M", (), {"message_id": 1})()

    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    asyncio.run(bot._send_text(1, "<b>жирный</b> текст"))
    assert sent == ["жирный текст"]


def test_daily_calls_counter(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    day = "2026-11-23"
    assert repo.daily_calls(day) == 0
    assert repo.add_daily_calls(5, day) == 5
    assert repo.add_daily_calls(3, day) == 8
    assert repo.daily_calls(day) == 8
    assert repo.daily_calls("2026-11-24") == 0


def test_cleanup_removes_old_records(tmp_path):
    import time as _time
    con, repo = _repo_with_post(tmp_path)
    old = _time.time() - 30 * 86400
    repo.enqueue(1)
    repo.finish(1, "done")
    con.execute("UPDATE fc_jobs SET updated=?", (old,))
    con.execute("INSERT INTO fc_runs(qid, cycle, stage, model, query, usage_json, ms, ts) "
                "VALUES(1, 0, 'post', '', '', '{}', 1, ?)", (old,))
    con.execute("INSERT INTO evidence(claim_key, url, domain, publisher_group, title, "
                "quote, status, ts) VALUES('k', 'https://x/a', 'x', 'g', 't', 'q', "
                "'supported', ?)", (old,))
    con.commit()
    assert repo.cleanup(14) == 3
    assert repo.pending() == 0


def test_job_state_transitions(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    repo.enqueue(1)
    repo.claim_next()
    assert repo.job_state(1) == "running"
    repo.fail(1, "boom")
    assert repo.job_state(1) == "retry"
    repo.finish(1, "error")
    assert repo.job_state(1) == "error"


# ---------- покрытие источников ----------
def test_disclaimer_suppressed_when_coverage_is_low():
    low = PostVerdict(status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                     coverage=3)])
    ok = PostVerdict(status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                     coverage=5)])
    assert low.disclaimer is None      # источников мало — вывода не делаем
    assert ok.disclaimer is not None


def test_claims_carry_ai_queries():
    payload = {"claims": [{
        "text": "Российский корвет Меркурий затонул в порту Бенгази",
        "ctype": "event", "priority": "core", "entities": ["Меркурий", "Бенгази"],
        "numbers": [], "lang": "en",
        "queries": ["Russian corvette Mercury sank Benghazi port",
                    "корвет Меркурий затонул Бенгази",
                    "Mercury corvette explosion Libya"]}]}
    extractor = ClaimExtractor(FakeLlm(payload), FakePrompts(), max_claims=3)
    claims = asyncio.run(extractor.extract("текст"))
    assert len(claims[0].search_queries) == 3
    assert claims[0].search_queries[0].startswith("Russian corvette")


def test_initial_queries_prefer_ai_written():
    claim = core_claim("Корвет затонул")
    claim.search_queries = ["Russian corvette sank Benghazi", "корвет затонул Бенгази"]
    queries = build_initial(claim, max_queries=5)
    texts = [q.text for q in queries]
    assert texts[0] == "Russian corvette sank Benghazi"
    assert "корвет затонул Бенгази" in texts


def test_service_records_source_coverage():
    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    hits = [Hit(url="https://www.reuters.com/a", title="R"),
            Hit(url="https://www.bbc.com/b", title="B"),
            Hit(url="https://meduza.io/c", title="M")]
    docs = {
        "https://www.reuters.com/a": Document(url="https://www.reuters.com/a",
                                              text="Drone attacks nearly halved refining."),
        "https://www.bbc.com/b": Document(url="https://www.bbc.com/b",
                                          text="Drone strikes nearly halved refining."),
        "https://meduza.io/c": Document(url="https://meduza.io/c",
                                        text="Атаки дронов снизили переработку."),
    }
    judge = FakeJudge([ClaimVerdict(claim=claim, status=Status.NOT_FOUND, note="мало")])
    service = _service(make_settings(fc_top_hits=3), CLAIM_PAYLOAD, hits, docs, judge)
    verdict = asyncio.run(service.check_text("текст", tier="t1"))
    assert verdict.claims[0].coverage >= 3


def test_repo_persists_coverage(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    repo.save(1, PostVerdict(qid=1, status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim(), status=Status.NOT_FOUND, coverage=5)]),
        version="v")
    loaded = repo.load(1)
    assert loaded.claims[0].coverage == 5


def test_factory_builds_service_without_errors():
    """Ловит ошибки сборки (импорты/конфиг), которые не видны в тестах сервиса."""
    from app.factcheck.factory import build_service

    settings = Settings(llm_key="test-key", fc_min_coverage=5, searx_url="http://searx:8080")
    service, llm = build_service(settings, repo=None)
    assert service is not None and llm is not None
    assert policy.MIN_COVERAGE == 5


# ---------- виджет фактчекинга ----------
def test_widget_shows_score_sources_and_mismatch():
    from app.factcheck.widget import render_widget

    ok = ClaimVerdict(claim=core_claim("ЦАХАЛ заявил об ударе", ctype=ClaimType.ATTRIBUTED),
                      status=Status.SUPPORTED, confidence=0.9, coverage=6,
                      evidence=[evidence_for("reuters.com"), evidence_for("bbc.com")])
    bad = ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                       coverage=6, note="источники называют 12",
                       evidence=[evidence_for("reuters.com")])
    part = ClaimVerdict(claim=core_claim("Удар был в четверг", ctype=ClaimType.DATE),
                        status=Status.PARTIAL, confidence=0.8, coverage=6)
    text = render_widget(PostVerdict(qid=1, claims=[ok, bad, part]))
    assert "Фактчекинг" in text
    assert "75%" in text
    assert "reuters.com" in text and "bbc.com" in text
    assert "Что не совпало" in text
    assert "12" in text


def test_widget_marks_insufficient_coverage_without_mismatch():
    from app.factcheck.widget import render_widget

    weak = ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                        coverage=2)
    text = render_widget(PostVerdict(qid=1, claims=[weak]))
    assert "недостаточно данных" in text
    assert "источников" in text
    assert "Что не совпало" not in text


def test_pending_widget_is_shown(monkeypatch):
    bot, _store = _stub_bot()
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append(text)
        return type("M", (), {"message_id": 1})()

    monkeypatch.setattr(bot.bot, "send_message", fake_send)
    asyncio.run(bot._send_widget(1, 42, None))
    assert sent and "проверка идёт" in sent[0]


def test_search_sends_engine_set():
    from app.factcheck.searxng import SearxngSearch
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080", engines="bing,brave,yandex")
    parts = []

    async def fake_get(params):
        parts.append(params)
        return {"results": [], "unresponsive_engines": []}

    client._get = fake_get
    asyncio.run(client.search(Query(text="x")))
    assert parts[0]["engines"] == "bing,brave,yandex"


def test_statistical_source_confirms_numeric_claim():
    claim = core_claim("Переводы в Мексику составили 65,5 млрд", ctype=ClaimType.NUMERIC,
                       numbers=("65,5",))
    stat = Evidence(url="https://banxico.org.mx/a", domain="banxico.org.mx",
                    publisher_group="stat-official")
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                           coverage=6, evidence=[stat])
    assert policy.confirmation_ok(verdict) is True

    wrong = ClaimVerdict(claim=claim, status=Status.REFUTED, confidence=0.9,
                         coverage=6, evidence=[stat])
    assert policy.correction_eligible(wrong, min_confidence=0.7) is True


def test_statistical_source_cannot_confirm_quote():
    claim = core_claim("Министр заявил о росте переводов",
                       ctype=ClaimType.ATTRIBUTED)
    stat = Evidence(url="https://worldbank.org/a", domain="worldbank.org",
                    publisher_group="stat-official")
    verdict = ClaimVerdict(claim=claim, status=Status.SUPPORTED, confidence=0.9,
                           coverage=6, evidence=[stat])
    assert policy.confirmation_ok(verdict) is False


def test_widget_separates_independent_and_statistics():
    from app.factcheck.widget import render_widget

    claim = core_claim("Переводы в Мексику 65,5 млрд", ctype=ClaimType.NUMERIC,
                       numbers=("65,5",))
    verdict = PostVerdict(qid=1, claims=[
        ClaimVerdict(claim=claim, status=Status.PARTIAL, confidence=0.8, coverage=6,
                     evidence=[evidence_for("reuters.com"),
                               Evidence(url="https://banxico.org.mx/a",
                                        domain="banxico.org.mx",
                                        publisher_group="stat-official")])])
    text = render_widget(verdict)
    assert "независимых: 1" in text
    assert "статистика: 1" in text
    assert "(стат.)" in text and "banxico.org.mx" in text


# ---------- источники и English-first ----------
def test_worldbank_lookup_returns_stat_snippet():
    from app.factcheck.worldbank import WorldBankStats

    wb = WorldBankStats()
    wb._fetch = lambda url: [{"page": 1}, [{
        "country": {"value": "Mexico"}, "date": "2025", "value": 65086796940}]]
    claim = Claim(idx=0, text="Money transfers from the USA to Mexico in 2025 "
                              "reached 65.5 billion USD",
                  ctype=ClaimType.NUMERIC, priority=Priority.CORE, numbers=["65.5"])
    snippets = asyncio.run(wb.lookup(claim))
    assert snippets
    assert snippets[0].domain == "worldbank.org"
    assert "65" in snippets[0].quote


def test_gdelt_parses_articles():
    from app.factcheck.gdelt import GdeltSearch

    gdelt = GdeltSearch()
    gdelt._fetch = lambda url: {"articles": [
        {"url": "https://example.com/a", "title": "T", "seendate": "20250101T000000Z"}]}
    hits = asyncio.run(gdelt.search("Mercury corvette"))
    assert hits and hits[0].engine == "gdelt"
    assert hits[0].published == "2025-01-01"


def test_english_translator_falls_back_to_source():
    from app.factcheck.localize import EnglishTranslator

    class BadLlm:
        async def text(self, **kwargs):
            raise RuntimeError("down")

    translator = EnglishTranslator(BadLlm(), FakePrompts())
    source = "Some source text that is definitely long enough."
    assert asyncio.run(translator.to_english(source)) == source


def test_service_translates_to_english_and_localizes():
    from app.factcheck.service import FactCheckService

    class FakeTranslator:
        def __init__(self):
            self.calls = 0

        async def to_english(self, text):
            self.calls += 1
            return "ENGLISH PIPELINE TEXT"

    class FakeLocalizer:
        def __init__(self):
            self.calls = 0

        async def localize(self, verdict):
            self.calls += 1
            return {"claims": [{"idx": 0, "text": "русский текст", "note": "русская заметка"}],
                    "corrections": []}

    claim = core_claim("Дроновые атаки почти вдвое снизили нефтепереработку в России",
                       ctype=ClaimType.EVENT, numbers=())
    judge = FakeJudge([ClaimVerdict(claim=claim, status=Status.NOT_FOUND)])
    translator, localizer = FakeTranslator(), FakeLocalizer()
    service = FactCheckService(
        settings=make_settings(), repo=None, search=FakeSearch(HITS),
        fetcher=FakeFetch(DOCS), judge=judge,
        extractor=ClaimExtractor(FakeLlm(CLAIM_PAYLOAD), FakePrompts(), max_claims=3),
        prompts=FakePrompts(), translator=translator, localizer=localizer)
    verdict = asyncio.run(service.check_text("исходный текст", tier="t1"))
    assert translator.calls == 1
    assert localizer.calls == 1
    assert verdict.localized["claims"][0]["text"] == "русский текст"


def test_repo_persists_en_text_and_localized(tmp_path):
    _con, repo = _repo_with_post(tmp_path)
    repo.save_en_text(1, "English text")
    assert repo.load_en_text(1) == "English text"
    repo.save(1, PostVerdict(qid=1, status=Status.NOT_FOUND, localized={
        "claims": [{"idx": 0, "text": "рус", "note": "заметка"}], "corrections": []},
        claims=[ClaimVerdict(claim=core_claim(), status=Status.NOT_FOUND)]), version="v")
    loaded = repo.load(1)
    assert loaded.localized["claims"][0]["text"] == "рус"


def test_widget_uses_localized_text():
    from app.factcheck.widget import render_widget

    verdict = PostVerdict(qid=1, status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                     coverage=6, evidence=[evidence_for("reuters.com")])])
    text = render_widget(verdict, localized={"claims": [
        {"idx": 0, "text": "Погибло 29 человек", "note": "нет данных"}], "corrections": []})
    assert "Погибло 29 человек" in text
    assert "нет данных" in text


def test_disclaimer_uses_localized_claim_text():
    verdict = PostVerdict(status=Status.NOT_FOUND, claims=[
        ClaimVerdict(claim=core_claim("Погибли 29 человек"), status=Status.NOT_FOUND,
                     coverage=5)])
    text = policy.render_disclaimer(verdict, claim_texts={0: "Погибло 29 человек"})
    assert "Погибло 29 человек" in text


def test_search_retries_without_time_range():
    from app.factcheck.searxng import SearxngSearch
    from app.factcheck.models import Query

    client = SearxngSearch("http://searx:8080", engines="bing,brave,mojeek")
    calls = []

    async def fake_get(params):
        calls.append(dict(params))
        if params.get("time_range"):
            return {"results": [], "unresponsive_engines": []}
        return {"results": [{"url": "https://www.reuters.com/a", "title": "t",
                             "content": "c", "engine": "bing", "score": 1.0}],
                "unresponsive_engines": []}

    client._get = fake_get
    hits = asyncio.run(client.search(Query(text="x", language="en", time_range="week")))
    assert len(hits) == 1
    assert len(calls) == 2 and "time_range" not in calls[1]
