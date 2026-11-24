"""Клиент SearXNG (self-hosted, JSON API)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import aiohttp

from .models import Hit, Query

# Соцсети/видео/агрегаторы: для проверки фактов бесполезны, шума много.
DEFAULT_DENY = (
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "t.me", "telegram.me", "vk.com", "ok.ru", "tiktok.com", "pinterest.com",
    "reddit.com", "dzen.ru", "zen.yandex.ru",
    "rambler.ru", "news.rambler.ru", "mail.ru", "news.mail.ru", "yandex.ru",
    "msn.com", "news.google.com", "baidu.com", "baike.baidu.com", "zhihu.com",
    "pikabu.ru", "livejournal.com", "medium.com", "quora.com",
)


class SearchError(RuntimeError):
    pass


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _denied(domain: str, deny: tuple[str, ...]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in deny)


def _norm_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


class SearxngSearch:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        results: int = 12,
        concurrency: int = 2,
        user_agent: str = "tgnews-factcheck/1.0",
        deny: tuple[str, ...] = DEFAULT_DENY,
        engines: str = "bing,brave,yandex",
        fallback_engines: str = "bing",
        cache_ttl: float = 3600.0,
    ):
        self.engines = ",".join(e.strip() for e in engines.split(",") if e.strip())
        self.fallback_engines = tuple(
            engine.strip() for engine in fallback_engines.split(",") if engine.strip()
        )
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, list[Hit]]] = {}
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.results = results
        self.deny = deny
        self.user_agent = user_agent
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._session: aiohttp.ClientSession | None = None
        self._warned: set[tuple] = set()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, parent: dict) -> dict:
        async with self._sem:
            try:
                session = await self._sess()
                async with session.get(self.base_url + "/search", params=parent) as response:
                    if response.status != 200:
                        raise SearchError(f"SearXNG HTTP {response.status}")
                    return await response.json(content_type=None)
            except aiohttp.ClientError as exc:
                raise SearchError(f"SearXNG недоступен: {exc}") from exc
            except asyncio.TimeoutError as exc:
                raise SearchError("SearXNG таймаут") from exc

    async def search(self, query: Query) -> list[Hit]:
        key = (query.text, query.language, query.time_range)
        cached = self._cache.get(key)
        now = asyncio.get_running_loop().time()
        if cached and now - cached[0] < self.cache_ttl:
            return list(cached[1])

        parent = {"q": query.text, "format": "json", "safesearch": "0", "pageno": "1"}
        if self.engines:
            parent["engines"] = self.engines
        if query.language:
            parent["language"] = query.language
        if query.time_range:
            parent["time_range"] = query.time_range

        hits = await self._first_working(parent)
        self._cache[key] = (now, list(hits))
        return hits

    async def _first_working(self, parent: dict) -> list[Hit]:
        """Основной набор движков, затем резервные. Пусто по вине движков != пусто честно."""
        data = await self._get(parent)
        hits = self._parse(data)
        if hits:
            return hits
        if not data.get("unresponsive_engines"):
            return []  # движки живы, просто ничего не нашли
        unresponsive = data.get("unresponsive_engines")
        for engine in self.fallback_engines:
            data = await self._get({**parent, "engines": engine})
            hits = self._parse(data)
            if hits:
                return hits
            if not data.get("unresponsive_engines"):
                # резервный движок ответил — значит это честное «ничего не найдено»,
                # а не сбой инфраструктуры (иначе посты ложно уйдут в degraded)
                return []
        raise SearchError(f"все движки недоступны: {unresponsive}")

    def _parse(self, data: dict) -> list[Hit]:
        unresponsive = data.get("unresponsive_engines") or []
        if unresponsive:
            signature = tuple(
                tuple(item) if isinstance(item, (list, tuple)) else (str(item),)
                for item in unresponsive
            )
            if signature not in self._warned:
                self._warned.add(signature)
                print(f"[fc.search] движки не ответили: {unresponsive}")
        hits: list[Hit] = []
        seen: set[str] = set()
        for item in data.get("results") or []:
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            domain = domain_of(url)
            if not domain or _denied(domain, self.deny):
                continue
            norm = _norm_url(url)
            if norm in seen:
                continue
            seen.add(norm)
            hits.append(
                Hit(
                    url=url,
                    title=str(item.get("title") or "")[:200],
                    snippet=str(item.get("content") or "")[:600],
                    engine=str(item.get("engine") or ""),
                    published=str(item.get("publishedDate") or item.get("pubdate") or "")
                    or None,
                    score=float(item.get("score") or 0),
                )
            )
            if len(hits) >= self.results:
                break
        return hits

    async def health(self) -> bool:
        try:
            await self.search(Query(text="test", language="en"))
            return True
        except Exception as exc:
            print(f"[fc.search] health: {exc}")
            return False
