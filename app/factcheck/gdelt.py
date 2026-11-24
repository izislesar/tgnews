"""GDELT 2.0 Doc API: бесплатный индекс мировых новостей, без ключа."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

from .models import Hit

API = "https://api.gdeltproject.org/api/v2/doc/doc"


class GdeltSearch:
    def __init__(self, *, timeout: float = 20.0, max_records: int = 20,
                 timespan: str = "14d"):
        self._disabled_until = 0.0
        self.timeout = timeout
        self.max_records = max_records
        self.timespan = timespan

    def _fetch(self, url: str) -> dict | None:
        request = urllib.request.Request(url, headers={"User-Agent": "tgnews/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    async def search(self, query: str) -> list[Hit]:
        import time as _time

        if not query.strip() or _time.time() < self._disabled_until:
            return []
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(self.max_records),
            "sort": "datedesc",
            "timespan": self.timespan,
        }
        url = f"{API}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            try:
                data = await asyncio.to_thread(self._fetch, url)
                break
            except Exception as exc:
                status = getattr(exc, "code", None)
                if status == 429:
                    # нас уже режет: не тратим время на каждый пост
                    self._disabled_until = _time.time() + 3600
                if status == 429 and attempt < 1:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                print(f"[gdelt] {type(exc).__name__}: {str(exc)[:100]}")
                return []
        else:
            return []
        hits: list[Hit] = []
        for article in (data or {}).get("articles", [])[: self.max_records]:
            link = article.get("url") or ""
            if not link.startswith(("http://", "https://")):
                continue
            raw = str(article.get("seendate") or "")
            published = (f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
                         if len(raw) >= 8 and raw[:8].isdigit() else None)
            hits.append(Hit(
                url=link,
                title=str(article.get("title") or "")[:200],
                snippet="",
                engine="gdelt",
                published=published,
            ))
        return hits
