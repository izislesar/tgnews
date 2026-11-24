"""Загрузка страниц: SSRF-защита, лимиты, trafilatura + фолбэк-извлечение, выбор сниппетов."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import aiohttp
import trafilatura
from lxml import html as lxml_html

from .models import Claim, Document, Snippet
from .searxng import domain_of

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml")
BLOCKED_HOSTS = ("localhost", "localhost.localdomain", "metadata.google.internal")
_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?…])\s+")


class FetchError(RuntimeError):
    pass


def _is_private_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _fallback_extract(raw: bytes) -> tuple[str, str]:
    """Когда trafilatura не справился: og:description + все абзацы."""
    try:
        tree = lxml_html.fromstring(raw)
    except Exception:
        return "", ""
    title = (tree.findtext(".//title") or "").strip()
    desc = ""
    for xpath in ('//meta[@property="og:description"]/@content',
                  '//meta[@name="description"]/@content'):
        found = tree.xpath(xpath)
        if found:
            desc = str(found[0]).strip()
            break
    paragraphs = []
    for node in tree.iter("p"):
        text = _WS.sub(" ", node.text_content()).strip()
        if len(text) >= 40:
            paragraphs.append(text)
    # таблицы: строки вида «2000 | 10.5» — без них статистика теряется
    for row in tree.iter("tr"):
        cells = [_WS.sub(" ", cell.text_content()).strip()
                 for cell in row.iter("td", "th")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            line = " | ".join(cells)
            if len(line) >= 8:
                paragraphs.append(line)
    body = "\n".join(paragraphs[:40])
    return title, (desc + "\n\n" + body).strip()


def extract_document(url: str, raw: bytes, *, min_text: int = 200) -> Document | None:
    """Чистая функция: байты страницы -> документ (тестируется без сети)."""
    text, title, lang, published, sitename = "", "", None, None, None
    # таблицы включаем только на небольших страницах: на гигантских таблицах
    # lxml/trafilatura раздувают память и роняют контейнер на 1 vCPU
    include_tables = len(raw) <= 500_000
    for favor_precision in (True, False):
        try:
            doc = trafilatura.bare_extraction(
                raw,
                url=url,
                with_metadata=True,
                include_comments=False,
                include_tables=include_tables,
                favor_precision=favor_precision,
            )
        except Exception:
            doc = None
        if doc is None:
            continue
        candidate = (doc.text or "").strip()
        if len(candidate) > len(text):
            text = candidate
            title = (doc.title or "").strip() or title
            lang = doc.language or lang
            published = doc.date or published
            sitename = doc.sitename or sitename
        if len(text) >= min_text:
            break

    if len(text) < min_text:
        fb_title, fb_text = _fallback_extract(raw)
        if len(fb_text) > len(text):
            text = fb_text
            title = title or fb_title

    if len(text) < 40:
        return None
    return Document(
        url=url,
        text=text[:20_000],
        title=title[:300],
        lang=lang,
        published=published,
        sitename=sitename,
    )


def _number_windows(text: str, numbers: list[str], *, width: int = 700) -> list[str]:
    """Окна вокруг самих чисел: «10,5» может стоять в таблице, а не в тексте."""
    low = text.lower()
    out: list[str] = []
    for number in numbers:
        raw = number.replace(" ", "")
        variants = {raw, raw.replace(",", "."), raw.replace(".", ",")}
        for variant in variants:
            if not variant:
                continue
            index = low.find(variant.lower())
            if index < 0:
                continue
            start = max(0, index - width // 2)
            end = min(len(text), index + width // 2)
            out.append(text[start:end])
            break
    return out


def select_snippets(doc: Document, claim: Claim, *, k: int = 3,
                    max_len: int = 480) -> list[Snippet]:
    """Окна вокруг терминов утверждения: дешевле и точнее, чем весь текст."""
    domain = domain_of(doc.url)
    sentences = [s.strip() for s in _SENT.split(_WS.sub(" ", doc.text)) if s.strip()]
    windows: list[str] = []
    buffer = ""
    for sentence in sentences:
        if not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) + 1 <= max_len:
            buffer = f"{buffer} {sentence}"
        else:
            windows.append(buffer)
            buffer = sentence
    if buffer:
        windows.append(buffer)

    claim_tokens = {w for w in re.findall(r"\w{3,}", claim.text.lower())}
    claim_tokens |= {w.lower() for w in claim.entities}
    numbers = [n.replace(" ", "") for n in claim.numbers if n.strip()]

    scored: list[tuple[float, str]] = []
    for window in windows[:200]:
        low = window.lower()
        tokens = set(re.findall(r"\w{3,}", low))
        if not tokens:
            continue
        hits = len(tokens & claim_tokens)
        num_hits = sum(3 for n in numbers if n and n in low.replace(" ", ""))
        score = (hits + num_hits) / (len(tokens) ** 0.5)
        if score > 0:
            scored.append((score, window))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    out: list[Snippet] = []
    seen: set[str] = set()
    # Лид статьи почти всегда содержит суть события, даже если он не пересекается
    # с формулировкой утверждения по словам. Без него судья часто «не видит» факт.
    if windows:
        lead = windows[0]
        out.append(Snippet(url=doc.url, domain=domain, quote=lead, title=doc.title,
                           published=doc.published))
        seen.add(lead[:60].lower())
    # для числовых утверждений сначала окна вокруг самих чисел (в т.ч. строк таблиц)
    for window in _number_windows(doc.text, numbers):
        fingerprint = window[:60].lower()
        if fingerprint in seen or len(out) >= k:
            continue
        seen.add(fingerprint)
        out.append(Snippet(url=doc.url, domain=domain, quote=window, title=doc.title,
                           published=doc.published))
    for _, window in scored:
        fingerprint = window[:60].lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(
            Snippet(url=doc.url, domain=domain, quote=window, title=doc.title,
                    published=doc.published)
        )
        if len(out) >= k:
            break
    if not out and windows:
        out.append(
            Snippet(url=doc.url, domain=domain, quote=windows[0][:max_len],
                    title=doc.title, published=doc.published)
        )
    return out


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: float = 12.0,
        max_bytes: int = 2_000_000,
        concurrency: int = 4,
        min_interval: float = 1.0,
        user_agent: str = "tgnews-factcheck/1.0",
        min_text: int = 200,
        max_redirects: int = 3,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_interval = min_interval
        self.user_agent = user_agent
        self.min_text = min_text
        self.max_redirects = max_redirects
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, sock_read=self.timeout),
                headers={"User-Agent": self.user_agent, "Accept-Language": "en,ru;q=0.8"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _validate(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise FetchError(f"схема запрещена: {parsed.scheme}")
        host = (parsed.hostname or "").lower()
        if not host:
            raise FetchError("пустой хост")
        if host in BLOCKED_HOSTS or host.endswith((".local", ".internal")):
            raise FetchError(f"хост запрещён: {host}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise FetchError(f"DNS не разрешился: {host}") from exc
        for info in infos:
            if _is_private_ip(info[4][0]):
                raise FetchError(f"хост ведёт в приватную сеть: {host} -> {info[4][0]}")
        return host

    async def _polite(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            wait = self._last.get(host, 0.0) + self.min_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = loop.time()

    async def _read_capped(self, response: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(65_536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= self.max_bytes:
                break
        return b"".join(chunks)

    async def fetch(self, url: str) -> Document | None:
        async with self._sem:
            current = url
            for _ in range(self.max_redirects + 1):
                try:
                    host = await self._validate(current)
                except FetchError as exc:
                    print(f"[fc.fetch] {exc} ({current[:80]})")
                    return None
                await self._polite(host)
                session = await self._sess()
                try:
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                return None
                            current = urljoin(current, location)
                            continue
                        if response.status != 200:
                            print(f"[fc.fetch] HTTP {response.status} {current[:80]}")
                            return None
                        ctype = (response.headers.get("Content-Type") or "").lower()
                        if not any(allowed in ctype for allowed in ALLOWED_CONTENT_TYPES):
                            return None
                        raw = await self._read_capped(response)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    print(f"[fc.fetch] {type(exc).__name__} {current[:80]}")
                    return None
                # trafilatura/lxml — CPU-bound: в отдельный поток, иначе блокируем
                # event loop бота (Telegram polling и таймеры встают)
                return await asyncio.to_thread(extract_document, current, raw,
                                               min_text=self.min_text)
            return None
