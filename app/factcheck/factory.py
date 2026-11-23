"""Сборка сервиса фактчекинга из настроек (используется ботом и CLI)."""

from __future__ import annotations

from ..llm_client import LlmClient, Prompts
from . import policy
from ..settings import Settings
from .claims import ClaimExtractor
from .correct import LlmCorrector
from .fetch import HttpFetcher
from .judge import LlmJudge
from .repo import FactcheckRepo
from .searxng import SearxngSearch
from .service import FactCheckService


def build_prompts(settings: Settings) -> Prompts:
    return Prompts(settings.prompts_dir or None)


def build_llm(settings: Settings) -> LlmClient:
    return LlmClient(settings)


def build_service(
    settings: Settings,
    repo: FactcheckRepo | None,
    *,
    prompts: Prompts | None = None,
    llm: LlmClient | None = None,
) -> tuple[FactCheckService, LlmClient]:
    policy.configure(min_coverage=settings.fc_min_coverage)
    prompts = prompts or build_prompts(settings)
    llm = llm or build_llm(settings)
    service = FactCheckService(
        settings=settings,
        repo=repo,
        search=SearxngSearch(
            settings.searx_url,
            timeout=settings.searx_timeout,
            results=settings.search_results,
            concurrency=settings.search_concurrency,
            fallback_engines=settings.searx_fallback_engines,
        ),
        fetcher=HttpFetcher(
            timeout=settings.fetch_timeout,
            max_bytes=settings.fetch_max_bytes,
            concurrency=settings.fetch_concurrency,
            min_interval=settings.fetch_min_interval,
            user_agent=settings.user_agent,
            min_text=settings.fc_min_text,
        ),
        judge=LlmJudge(llm, prompts, model=settings.judge_model),
        extractor=ClaimExtractor(
            llm, prompts, max_claims=settings.fc_max_claims, model=settings.claim_model
        ),
        corrector=LlmCorrector(
            llm, prompts, model=settings.model,
            min_confidence=settings.fc_fix_min_conf,
        ),
        fix_facts=settings.fc_fix_facts,
        prompts=prompts,
    )
    return service, llm
