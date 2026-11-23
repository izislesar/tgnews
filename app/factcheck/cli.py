"""CLI: проверить пост вручную, без бота.

    python -m app.factcheck.cli --qid 123
    python -m app.factcheck.cli --text "Текст поста" --tier t2
    python -m app.factcheck.cli --qid 123 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys

from ..llm_client import LlmClient, Prompts
from ..settings import Settings
from .claims import ClaimExtractor
from .fetch import HttpFetcher
from .judge import LlmJudge
from .policy import render_evidence
from .repo import FactcheckRepo
from .searxng import SearxngSearch
from .service import FactCheckService


def build_service(settings: Settings):
    from .factory import build_service as factory_build

    return factory_build(settings, repo=None)


def _load_text(db: str, qid: int) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT text FROM posts WHERE qid=?", (qid,)).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"пост qid={qid} в {db} не найден")
    return row[0] or ""


def _short_url(url: str, limit: int = 86) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"


def _print_human(verdict, text: str) -> None:
    print("=" * 78)
    print(f"ПОСТ: {' '.join(text.split())[:300]}")
    print("=" * 78)
    for item in verdict.claims:
        flag = "" if item.claim.is_core else " (secondary)"
        print(f"\n[{item.claim.ctype.value}/{item.claim.priority.value}]{flag} "
              f"«{item.claim.text}»")
        print(f"  -> {item.status.value} conf={item.confidence:.2f} "
              f"источников={item.coverage} cycles={item.cycles} "
              f"note={item.note[:140]!r}")
        if item.error:
            print(f"  error: {item.error}")
        for evidence in item.evidence:
            print(f"  + {evidence.publisher_group:18s} {_short_url(evidence.url)}")
    print("\n" + "-" * 78)
    print(f"ИТОГ: {verdict.status.value} | вызовов LLM: {verdict.calls} | "
          f"{verdict.elapsed_s}s")
    disclaimer = verdict.disclaimer
    print(f"ДИСКЛЕЙМЕР: {disclaimer or '— (не нужен)'}")
    if verdict.corrections:
        print("\nИСПРАВЛЕНО (вариант 2):")
        for item in verdict.corrections:
            print(f"  былo: {item.old[:90]}")
            print(f"  стало: {item.new[:90]}  [{item.source_url[:70]}]")
    if verdict.corrected_text:
        print("\nПЕРЕПИСАННЫЙ ПОСТ:\n" + verdict.corrected_text[:1200])
    evidence = render_evidence(verdict, limit=8)
    if evidence:
        print("\nКАК ПРОВЕРЯЛИ:")
        print("\n".join(evidence))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Фактчекинг поста по открытым источникам")
    parser.add_argument("--qid", type=int, help="qid поста в БД")
    parser.add_argument("--text", help="проверить произвольный текст")
    parser.add_argument("--db", default=None, help="путь к queue.db")
    parser.add_argument("--env-file", default=None, help="путь к .env")
    parser.add_argument("--tier", default="t2", choices=["t1", "t2"])
    parser.add_argument("--cycles", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--save", action="store_true", help="сохранить вердикт в БД")
    parser.add_argument("--fix", action="store_true",
                        help="переписать пост под данные независимых источников")
    args = parser.parse_args(argv)

    settings = Settings.load(args.env_file)
    if args.db:
        object.__setattr__(settings, "db_path", args.db)
    if args.cycles:
        object.__setattr__(settings, "fc_cycles", args.cycles)

    if args.text:
        text = args.text
    elif args.qid:
        text = _load_text(settings.db_path, args.qid)
    else:
        parser.error("нужен --qid или --text")

    async def run():
        service, llm = build_service(settings)
        try:
            verdict = await service.check_text(text, qid=args.qid, tier=args.tier,
                                              fix_facts=True if args.fix else None)
        finally:
            await llm.close()
            await service.search.close()
            await service.fetcher.close()
        return verdict

    verdict = asyncio.run(run())

    if args.save and args.qid:
        con = sqlite3.connect(settings.db_path)
        try:
            repo = FactcheckRepo(con)
            repo.save(args.qid, verdict, version=service_version())
        finally:
            con.close()

    if args.json:
        print(json.dumps(json.loads(verdict.model_dump_json()), ensure_ascii=False, indent=2))
    else:
        _print_human(verdict, text)
    return 0


def service_version() -> str:
    try:
        return Prompts().version()
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
