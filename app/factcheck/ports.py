"""Порты внешнего мира. В тестах подменяются фейками — сети и LLM там нет."""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import Claim, ClaimVerdict, Document, Hit, Query, Snippet


class SearchPort(Protocol):
    async def search(self, query: Query) -> list[Hit]: ...


class FetchPort(Protocol):
    async def fetch(self, url: str) -> Document | None: ...


class JudgePort(Protocol):
    async def judge(
        self, claim: Claim, snippets: Sequence[Snippet], *, cycle: int
    ) -> ClaimVerdict: ...


class ClockPort(Protocol):
    def now(self) -> float: ...

    def monotonic(self) -> float: ...
