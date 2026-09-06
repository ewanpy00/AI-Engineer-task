"""DTO клиента Metacritic — контракты из design §4.1.

Всё, что приходит из API, оседает здесь: дальше по коду сырых dict'ов нет.
Единственное исключение — `Product.raw`: он сохраняется в `games.raw` целиком.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Audience = Literal["critic", "user"]
Bucket = Literal["positive", "neutral", "negative", "default"]


@dataclass(frozen=True)
class CatalogItem:
    """Элемент finder-эндпоинтов (new releases и browse)."""

    id: int
    slug: str
    title: str
    release_date: date | None = None


@dataclass(frozen=True)
class BrowsePage:
    items: list[CatalogItem]
    offset: int
    total: int


@dataclass(frozen=True)
class PlatformInfo:
    slug: str
    name: str
    is_lead: bool
    release_date: date | None = None
    metascore: int | None = None
    metascore_count: int | None = None
    metascore_sentiment: str | None = None


@dataclass(frozen=True)
class Product:
    id: int
    slug: str
    title: str
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    esrb_rating: str | None = None
    release_date: date | None = None
    cover_path: str | None = None
    video_url: str | None = None
    genres: list[str] = field(default_factory=list)
    platforms: list[PlatformInfo] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def lead_platform(self) -> PlatformInfo | None:
        return next((p for p in self.platforms if p.is_lead), None)


@dataclass(frozen=True)
class ScoreStats:
    """Сводка оценок по одной платформе. Шкала критиков 0-100, пользователей 0-10."""

    score: float | None
    count: int | None
    sentiment: str | None


@dataclass(frozen=True)
class Quote:
    """НЕДОВЕРЕННЫЙ ВВОД (design §5.3): текст пользователя/издания.

    Никогда не подставляется в промпт как инструкция — только как данные
    в отдельном сообщении, обёрнутые в <review>.
    """

    text: str
    score: float | None = None
    author: str | None = None
    bucket: Bucket = "default"


@dataclass(frozen=True)
class ReviewQuotes:
    """Готовая подборка цитат с summary-эндпоинта, разложенная по тональности."""

    default: list[Quote] = field(default_factory=list)
    positive: list[Quote] = field(default_factory=list)
    neutral: list[Quote] = field(default_factory=list)
    negative: list[Quote] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.default or self.positive or self.neutral or self.negative)


@dataclass(frozen=True)
class Review:
    """Элемент полного списка отзывов — fallback, когда summary пуст."""

    text: str
    score: float | None = None
    author: str | None = None
    publication: str | None = None
    date: date | None = None

    def to_quote(self, bucket: Bucket = "default") -> Quote:
        return Quote(text=self.text, score=self.score, author=self.author or self.publication, bucket=bucket)
