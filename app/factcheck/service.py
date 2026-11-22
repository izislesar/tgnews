"""Оркестратор: циклы проверки, дедлайн, бюджет, очередь с лизом."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..llm_client import Prompts
from ..settings import Settings
from . import policy
from . import query as query_mod
from .claims import ClaimExtractor
from .fetch import HttpFetcher, select_snippets
from .judge import LlmJudge
from .models import (
    Claim,
    ClaimType,
    ClaimVerdict,
    Document,
    Hit,
    PostVerdict,
    Snippet,
    Status,
)
from .repo import FactcheckRepo
from .searxng import SearxngSearch, domain_of


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int
    calls: int = 0

    def take(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"бюджет LLM исчерпан ({self.max_calls} вызовов)")
        self.calls += 1


class FactCheckService:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: FactcheckRepo | None,
        search: SearxngSearch,
        fetcher: HttpFetcher,
        judge: LlmJudge,
        extractor: ClaimExtractor,
        prompts: Prompts,
        clock=time.monotonic,
    ):
        self.settings = settings
        self.repo = repo
        self.search = search
        self.fetcher = fetcher
        self.judge = judge
        self.extractor = extractor
        self.prompts = prompts
        self.clock = clock

    # ---------- одна проверка ----------
    async def check_text(
        self,
        text: str,
        *,
        qid: int | None = None,
        tier: str = "t1",
        post_ts: float | None = None,
    ) -> PostVerdict:
        started = self.clock()
        max_calls = self.settings.fc_calls_per_post * (2 if tier == "t2" else 1)
        budget = Budget(max_calls)
        deadline = started + self.settings.fc_deadline_s

        claims = await self.extractor.extract(text)
        budget.calls += 1

        secondary = [
            ClaimVerdict(claim=claim, status=Status.SKIPPED, note="не core — не проверяем")
            for claim in claims
            if not claim.is_core or claim.ctype is ClaimType.OPINION
        ]
        core = [c for c in claims
                if c.is_core and c.ctype is not ClaimType.OPINION]

        if not core:
            return PostVerdict(
                status=Status.SKIPPED,
                claims=secondary,
                qid=qid,
                note="нет core-утверждений",
                calls=budget.calls,
                elapsed_s=round(self.clock() - started, 2),
            )

        cycles = 1 if tier == "t1" else max(1, self.settings.fc_cycles)
        semaphore = asyncio.Semaphore(max(1, self.settings.fc_judge_concurrency))

        async def run(claim: Claim) -> ClaimVerdict:
            async with semaphore:
                try:
                    return await self._check_claim(claim, cycles, deadline, budget, post_ts)
                except BudgetExceeded as exc:
                    return ClaimVerdict(claim=claim, status=Status.NOT_FOUND, error=str(exc))
                except Exception as exc:  # одна упавшая проверка не роняет пост
                    print(f"[fc] claim {claim.idx}: {type(exc).__name__}: {exc}")
                    return ClaimVerdict(claim=claim, status=Status.NOT_FOUND,
                                        error=f"{type(exc).__name__}: {str(exc)[:120]}")

        checked = list(await asyncio.gather(*(run(claim) for claim in core)))
        all_claims = checked + secondary
        errored = [item for item in checked if item.error]
        degraded = bool(checked) and len(errored) == len(checked)
        verdict = PostVerdict(
            status=policy.aggregate(checked),
            claims=all_claims,
            qid=qid,
            calls=budget.calls,
            elapsed_s=round(self.clock() - started, 2),
            degraded=degraded,
        )
        if degraded:
            verdict.note = "проверка не удалась: поиск/загрузка недоступны"
        elif errored:
            verdict.note = f"часть проверок не удалась ({len(errored)} из {len(checked)})"
        else:
            verdict.note = verdict.disclaimer or ""
        return verdict

    async def _check_claim(
        self,
        claim: Claim,
        cycles: int,
        deadline: float,
        budget: Budget,
        post_ts: float | None,
    ) -> ClaimVerdict:
        queries = query_mod.build_initial(claim, post_ts=post_ts)
        seen_urls: set[str] = set()
        best: ClaimVerdict | None = None

        for cycle in range(1, cycles + 1):
            if self.clock() >= deadline:
                break
            hits: list[Hit] = []
            attempts = failures = 0
            for query in queries:
                if self.clock() >= deadline:
                    break
                attempts += 1
                try:
                    found = await self.search.search(query)
                except Exception as exc:
                    failures += 1
                    print(f"[fc.search] {exc}")
                    continue
                hits.extend(hit for hit in found if hit.url not in seen_urls)

            hits = _dedupe_hits(hits)[: self.settings.fc_top_hits]
            if not hits:
                if attempts and failures == attempts:
                    # Это не «не подтверждено», а «не смогли проверить».
                    best = best or ClaimVerdict(claim=claim, status=Status.NOT_FOUND,
                                                error="поиск недоступен", cycles=cycle)
                else:
                    best = best or ClaimVerdict(claim=claim, status=Status.NOT_FOUND,
                                                note="поиск ничего не дал", cycles=cycle)
                break

            seen_urls.update(hit.url for hit in hits)
            docs = await asyncio.gather(*(self.fetcher.fetch(hit.url) for hit in hits),
                                        return_exceptions=True)
            snippets: list[Snippet] = []
            for hit, doc in zip(hits, docs):
                if isinstance(doc, Document):
                    snippets.extend(select_snippets(doc, claim,
                                                    k=self.settings.fc_snippets_per_claim))
                elif hit.snippet:
                    snippets.append(Snippet(url=hit.url, domain=domain_of(hit.url),
                                            quote=hit.snippet, title=hit.title,
                                            published=hit.published))
            snippets = _dedupe_snippets(snippets)[: self.settings.fc_snippets_per_claim * 2]
            if not snippets:
                best = best or ClaimVerdict(claim=claim, status=Status.NOT_FOUND,
                                            note="страницы не извлеклись", cycles=cycle)
                break

            budget.take()
            post_date = (datetime.fromtimestamp(post_ts, tz=timezone.utc).strftime("%d.%m.%Y")
                         if post_ts else None)
            verdict = await self.judge.judge(claim, snippets, cycle=cycle,
                                             post_date=post_date)
            verdict.queries = [query.text for query in queries]
            best = _stronger(best, verdict)
            if verdict.status is Status.SUPPORTED and policy.confirmation_ok(verdict):
                break
            if (verdict.status is Status.REFUTED
                    and verdict.confidence >= policy.MIN_CONFIDENCE):
                break
            queries = query_mod.refine(claim, verdict, cycle)

        return best or ClaimVerdict(claim=claim, status=Status.NOT_FOUND, note="не проверено")

    # ---------- очередь ----------
    async def run_forever(self, *, poll_s: float = 5.0, on_done=None) -> None:
        if self.repo is None:
            raise RuntimeError("run_forever без очереди (repo=None)")
        while True:
            qid = None
            try:
                qid = self.repo.claim_next()
                if qid is None:
                    await asyncio.sleep(poll_s)
                    continue
                text = self.repo.post_text(qid)
                if not text:
                    self.repo.finish(qid, "skipped")
                    continue
                tier = self.repo.job_tier(qid) or "t1"
                verdict = await self.check_text(text, qid=qid, tier=tier,
                                                post_ts=self.repo.post_ts(qid))
                self.repo.save(qid, verdict, version=self.prompts.version())
                self.repo.finish(qid, "done")
                print(f"[fc] qid={qid} -> {verdict.status.value} "
                      f"({verdict.elapsed_s}s, {verdict.calls} вызовов)")
                if on_done is not None:
                    try:
                        await on_done(qid, verdict)
                    except Exception as exc:
                        print(f"[fc] on_done qid={qid}: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # цикл не должен умирать от одной ошибки БД/сети
                print(f"[fc] цикл qid={qid}: {type(exc).__name__}: {exc}")
                if qid is not None:
                    try:
                        self.repo.fail(qid, str(exc))
                    except Exception:
                        pass
                await asyncio.sleep(poll_s)


def _dedupe_hits(hits: list[Hit]) -> list[Hit]:
    seen: set[str] = set()
    out: list[Hit] = []
    for hit in hits:
        key = hit.url.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _dedupe_snippets(snippets: list[Snippet]) -> list[Snippet]:
    seen: set[str] = set()
    out: list[Snippet] = []
    for snippet in snippets:
        key = f"{snippet.url}|{snippet.quote[:60].lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(snippet)
    return out


def _stronger(current: ClaimVerdict | None, candidate: ClaimVerdict) -> ClaimVerdict:
    """Что считать лучшим результатом цикла.

    «Не найдено» — это отсутствие результата: более поздний supported/refuted его
    перекрывает. Между содержательными вердиктами побеждает более строгий
    (refuted > contested > partial > supported), при равенстве — более уверенный.
    """
    if current is None:
        return candidate
    current_found = current.status is not Status.NOT_FOUND
    candidate_found = candidate.status is not Status.NOT_FOUND
    if current_found != candidate_found:
        return candidate if candidate_found else current
    if policy.rank(candidate.status) > policy.rank(current.status):
        return candidate
    if policy.rank(candidate.status) == policy.rank(current.status):
        if candidate.confidence >= current.confidence:
            return candidate
    return current
