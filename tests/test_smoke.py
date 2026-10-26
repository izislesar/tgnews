"""Смоук-тесты CX-критичных частей: разметка, батч-парсер, чёрный список, дедуп, скоринг."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bot import fmt_html  # noqa: E402
from app.llm import _parse_batch  # noqa: E402
from app.store import _jaccard, _tokens, Store  # noqa: E402
from app.worker import BLACK_RE  # noqa: E402


def test_fmt_html_blockquote():
    t = "🗣 **Лид с атрибуцией:**\n\n>> «Цитата спикера целиком»\n\nИсточник: https://t.me/s/1\n"
    r = fmt_html(t)
    assert "<blockquote>«Цитата спикера целиком»</blockquote>" in r
    assert "<b>Лид с атрибуцией:</b>" in r
    assert '<a href="https://t.me/s/1">' in r


def test_fmt_html_expandable():
    long_q = "Очень длинная цитата " * 30
    r = fmt_html(f">> {long_q}")
    assert "<blockquote expandable>" in r


def test_fmt_html_unclosed_bold_fallback():
    r = fmt_html("**незакрытый жирный")
    assert "<b>" not in r and "**" in r


def test_batch_parser():
    out = "===1===\n🔥 **Пост один**\n===2===\nSKIP\n===3===\n📰 **Пост три**"
    assert _parse_batch(out, 4) == ["🔥 **Пост один**", None, "📰 **Пост три**", False]
    # маркеры источников ===SOURCE N=== не путают парсер
    src = "===SOURCE 1===\ntext\n===2===\nперевод"
    assert _parse_batch(src, 2) == [False, "перевод"]


def test_blacklist():
    block = [
        "Аргентина обыграла Испанию в финале чемпионата мира по футболу со счетом 3:2",
        "Реал Мадрид выиграл Лигу чемпионов",
        "Промокод на первый заказ",
        "Гороскоп на неделю для всех знаков зодиака",
        "Переходи по ссылке и получи бонус",
    ]
    for s in block:
        assert BLACK_RE.search(s), s
    keep = [
        "Курс биткоина перевалил за 79 тысяч долларов",
        "Умерла актриса Хайден Панеттьери — звезда «Героев»",
        "Село Куртовка перешло под контроль Армии России",
        "Минобороны сообщило об испытательном запуске баллистической ракеты",
        "ВТБ рекламирует вклад со ставкой до 15,21%",
    ]
    for s in keep:
        assert not BLACK_RE.search(s), s


def test_dedup_jaccard():
    a = _tokens("Удар БПЛА по нефтеперерабатывающему заводу в Рязани ночью")
    b = _tokens("Ночной удар БПЛА по нефтеперерабатывающему заводу в Рязани")
    assert _jaccard(a, b) >= 0.6
    c = _tokens("Курс биткоина вырос до 79 тысяч долларов")
    assert _jaccard(a, c) < 0.6


def test_store_candidates_and_decisions(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    st.add_channel("ch1", "t")
    st.add_channel_meta("ch1", "Channel One")
    st.set_subs("ch1", 100000)
    t0 = __import__("time").time()
    st.upsert_post(1, "ch1", "10", "k1", "Удар БПЛА по заводу в Рязани, 29 человек погибло", 90000, 500, 0)
    st.upsert_post(2, "ch1", "11", "k2", "Ночной удар БПЛА по заводу в Рязани, 29 человек погибло", 80000, 400, 0)
    rows = st.con.execute("SELECT state FROM posts ORDER BY qid").fetchall()
    assert rows == [("new",), ("dup",)]  # дедуп: вторая копия помечена

    cands = st.candidates(42, limit=5)
    assert len(cands) == 1 and cands[0][0] == 1
    st.mark_issued(1, 42)
    assert st.candidates(42) == []  # выданное админу не повторяется
    assert len(st.candidates(43)) == 1  # другому админу — предлагается

    st.add_decision(1, 43, "reject")
    assert st.candidates(43) == []  # отклонённое тоже не предлагается


def test_store_scores_small_channel():
    st = Store(":memory:")
    # мелкий канал с жирным постом должен обгонять гиганта с теми же views
    st.add_channel("small", "s")
    st.add_channel("big", "b")
    st.set_subs("small", 50000)
    st.set_subs("big", 1100000)
    t = __import__("time").time()
    st.upsert_post(1, "small", "1", "k1", "Удар БПЛА по заводу в Рязани ночью, 29 погибло", 30000, 200, 0)
    st.upsert_post(2, "big", "2", "k2", "Президент Франции выступил на саммите ЕС в Брюсселе", 30000, 200, 0)
    cands = st.candidates(1, limit=2)
    assert cands[0][1] == "small"  # нормировка: у small это 60% аудитории, у big — 2.7%
