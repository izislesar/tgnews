"""Модели данных фактчекинга: утверждение, источник, вердикт."""

from __future__ import annotations

import hashlib
import re
from enum import Enum

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    NUMERIC = "numeric"      # числа, доли, потери
    DATE = "date"            # даты и сроки
    EVENT = "event"          # «что произошло»
    QUOTE = "quote"          # прямая речь спикера
    ATTRIBUTED = "attributed"  # «X заявил, что Y» — проверяем только атрибуцию
    OPINION = "opinion"      # оценки — не проверяем, идут в нейтрализацию


class Priority(str, Enum):
    CORE = "core"            # дисклеймер ставится только по core
    SECONDARY = "secondary"


class Status(str, Enum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    CONTESTED = "contested"
    NOT_FOUND = "not_found"
    REFUTED = "refuted"
    SKIPPED = "skipped"      # не проверяли (мнение / выключено)


_WS = re.compile(r"\s+", re.UNICODE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


class Claim(BaseModel):
    idx: int
    text: str
    ctype: ClaimType = ClaimType.EVENT
    priority: Priority = Priority.SECONDARY
    entities: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    lang: str | None = None
    # поисковые запросы, которые пишет сама модель (EN / оригинал / RU)
    search_queries: list[str] = Field(default_factory=list)

    @property
    def claim_key(self) -> str:
        """Стабильный ключ: один кэш на все перепечатки одной новости."""
        norm = _PUNCT.sub(" ", _WS.sub(" ", self.text.lower())).strip()
        norm = " ".join(sorted({w for w in norm.split() if len(w) > 2}))
        raw = f"{self.ctype.value}|{norm}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @property
    def is_core(self) -> bool:
        return self.priority is Priority.CORE


class Query(BaseModel):
    text: str
    language: str | None = None
    time_range: str | None = None  # day | week | month | year


class Hit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""
    published: str | None = None
    score: float = 0.0


class Document(BaseModel):
    url: str
    text: str
    title: str = ""
    lang: str | None = None
    published: str | None = None
    sitename: str | None = None


class Snippet(BaseModel):
    url: str
    domain: str
    quote: str
    title: str = ""
    published: str | None = None


class Evidence(BaseModel):
    url: str
    domain: str
    publisher_group: str
    quote: str = ""
    title: str = ""


class CorrectionItem(BaseModel):
    """Одна правка: что было, что стало и на каком источнике основано."""

    claim_idx: int
    claim: str = ""
    old: str = ""
    new: str = ""
    source_url: str = ""


class ClaimVerdict(BaseModel):
    claim: Claim
    status: Status = Status.NOT_FOUND
    confidence: float = 0.0
    note: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    cycles: int = 0
    error: str | None = None
    corrected: bool = False
    coverage: int = 0  # сколько разных доменов реально просмотрено  # факт исправлен в тексте, метка по нему не нужна

    @property
    def groups(self) -> set[str]:
        return {e.publisher_group for e in self.evidence if e.publisher_group}


class PostVerdict(BaseModel):
    status: Status = Status.SKIPPED
    claims: list[ClaimVerdict] = Field(default_factory=list)
    qid: int | None = None
    note: str = ""
    calls: int = 0
    elapsed_s: float = 0.0
    degraded: bool = False  # поиск/фетч лежали: это не «не подтверждено», это «не проверили»
    corrected_text: str | None = None
    corrections: list[CorrectionItem] = Field(default_factory=list)

    @property
    def core_claims(self) -> list[ClaimVerdict]:
        return [c for c in self.claims if c.claim.is_core]

    @property
    def disclaimer(self) -> str | None:
        from .policy import render_disclaimer  # локальный импорт: избегаем цикла

        return render_disclaimer(self)
