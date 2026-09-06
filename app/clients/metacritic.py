"""Клиент `backend.metacritic.com`: транспорт (T-05) и методы API (T-06).

Транспорт — единый `AsyncClient`, глобальный rate-limit, retry с
экспоненциальным backoff и единственный тип исключения наружу.
Ниже, отдельным слоем, `MetacriticClient`: построение URL и маппинг в DTO.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from app.clients.dto import (
    Audience,
    Bucket,
    BrowsePage,
    CatalogItem,
    PlatformInfo,
    Product,
    Quote,
    Review,
    ReviewQuotes,
    ScoreStats,
)
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

BODY_EXCERPT_LIMIT = 500
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0


class MetacriticError(Exception):
    """Единственное исключение, которое клиент выпускает наружу.

    `status` = None, если до ответа дело не дошло (таймаут, обрыв соединения).
    """

    def __init__(self, status: int | None, url: str, body_excerpt: str) -> None:
        self.status = status
        self.url = url
        self.body_excerpt = body_excerpt
        super().__init__(f"Metacritic {status or 'no-response'} {url}: {body_excerpt}")


class RateLimiter:
    """Глобальный ограничитель: не более `rps` запросов в секунду на процесс.

    Глобальный, а не на эндпоинт: research не подтвердил лимиты API (RISK #4),
    поэтому считаем бюджет общим для всех вызовов.
    """

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


def _excerpt(response: httpx.Response) -> str:
    try:
        return response.text[:BODY_EXCERPT_LIMIT]
    except Exception:  # noqa: BLE001 — тело может быть недекодируемым
        return f"<{len(response.content)} bytes>"


def _retry_after_s(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date форму не разбираем, уходим на обычный backoff


class MetacriticTransport:
    """Транспорт: один `AsyncClient` на процесс, rate-limit и retry внутри."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.metacritic_base_url,
            timeout=self._settings.metacritic_timeout_s,
            headers={
                # Явный не-дефолтный UA: research зафиксировал WAF-блокировку
                # литералов python-requests/Java на www.metacritic.com
                "User-Agent": self._settings.metacritic_user_agent,
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        self._limiter = RateLimiter(self._settings.metacritic_rps)

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET с retry. Возвращает разобранный JSON или бросает `MetacriticError`."""
        attempts = max(1, self._settings.metacritic_max_retries)
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last: MetacriticError | None = None

        for attempt in range(1, attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(path, params=clean)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = MetacriticError(None, self._url(path, clean), repr(exc))
                await self._sleep_backoff(attempt, attempts, None)
                continue

            url = str(response.request.url)
            if response.status_code == 429 or response.status_code >= 500:
                last = MetacriticError(response.status_code, url, _excerpt(response))
                await self._sleep_backoff(attempt, attempts, _retry_after_s(response))
                continue
            if response.status_code >= 400:
                # Клиентские ошибки не ретраим: 404 от повтора игрой не станет
                raise MetacriticError(response.status_code, url, _excerpt(response))

            try:
                payload = response.json()
            except ValueError as exc:
                raise MetacriticError(response.status_code, url, _excerpt(response)) from exc
            if not isinstance(payload, dict):
                raise MetacriticError(
                    response.status_code, url, f"expected object, got {type(payload).__name__}"
                )
            return payload

        assert last is not None  # цикл всегда либо вернул, либо записал ошибку
        raise last

    async def _sleep_backoff(self, attempt: int, attempts: int, retry_after: float | None) -> None:
        if attempt >= attempts:
            return  # попытки кончились, спать перед выходом незачем
        delay = retry_after if retry_after is not None else BACKOFF_BASE_S * 2 ** (attempt - 1)
        delay = min(delay, BACKOFF_MAX_S) + random.uniform(0, 0.25)  # джиттер против синхронных ретраев
        log.warning("metacritic retry %s/%s через %.2fs", attempt, attempts, delay)
        await asyncio.sleep(delay)

    def _url(self, path: str, params: dict[str, Any]) -> str:
        return str(httpx.URL(self._settings.metacritic_base_url).join(path).copy_merge_params(params))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> MetacriticTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


# --- Маппинг ответов API в DTO (T-06) ---------------------------------------
#
# Пути полей зафиксированы в docs/00-research.json (раздел `endpoints`).
# Правило на всё, что ниже: отсутствующее поле — это None/пустой список,
# а не исключение. API недокументирован, форму может поменять без предупреждения,
# и терять игру целиком из-за пропавшего `sentiment` мы не хотим.


def quote_path(value: str) -> str:
    """Экранирование сегмента пути: slug из API в URL подставляется как есть."""
    return quote(value, safe="")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _item(payload: dict) -> dict:
    data = payload.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    return item if isinstance(item, dict) else {}


def _items(payload: dict) -> list[dict]:
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _total(payload: dict) -> int:
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0
    # browse отдаёт totalResults; research §endpoints обещал `total` — принимаем оба
    return _as_int(data.get("totalResults")) or _as_int(data.get("total")) or 0


def _catalog_item(raw: dict) -> CatalogItem | None:
    game_id, slug = _as_int(raw.get("id")), _as_str(raw.get("slug"))
    title = _as_str(raw.get("title"))
    if game_id is None or not slug or not title:
        return None  # без id/slug игру не заклеймить и не запросить — пропускаем
    return CatalogItem(id=game_id, slug=slug, title=title, release_date=_as_date(raw.get("releaseDate")))


def _company(item: dict, type_name: str) -> str | None:
    production = item.get("production")
    companies = production.get("companies") if isinstance(production, dict) else None
    if not isinstance(companies, list):
        return None
    for company in companies:
        if isinstance(company, dict) and company.get("typeName") == type_name:
            return _as_str(company.get("name"))
    return None


def _cover_path(item: dict) -> str | None:
    images = item.get("images")
    if not isinstance(images, list):
        return None
    by_type = {
        _as_str(img.get("typeName")): _as_str(img.get("bucketPath"))
        for img in images
        if isinstance(img, dict)
    }
    # cardImage — вертикальный постер 226x332, ровно то, что нужно карточке списка
    for type_name in ("cardImage", "mainImage"):
        if by_type.get(type_name):
            return by_type[type_name]
    return next((p for p in by_type.values() if p), None)


def _platforms(item: dict) -> list[PlatformInfo]:
    raw_platforms = item.get("platforms")
    if not isinstance(raw_platforms, list):
        return []
    parsed: list[dict] = []
    for raw in raw_platforms:
        if not isinstance(raw, dict):
            continue
        slug = _as_str(raw.get("slug"))
        if not slug:
            continue
        summary = raw.get("criticScoreSummary")
        summary = summary if isinstance(summary, dict) else {}
        parsed.append(
            {
                "slug": slug,
                "name": _as_str(raw.get("name")) or slug,
                "declared_lead": bool(raw.get("isLeadPlatform")),
                "release_date": _as_date(raw.get("releaseDate")),
                "metascore": _as_int(summary.get("score")),
                "metascore_count": _as_int(summary.get("reviewCount")),
                "metascore_sentiment": _as_str(summary.get("sentiment")),
            }
        )
    if not parsed:
        return []

    lead = next((i for i, p in enumerate(parsed) if p["declared_lead"]), None)
    if lead is None:
        # design §8: нет isLeadPlatform — берём платформу с наибольшим
        # metascore_count, при равенстве первую в массиве (max стабилен)
        lead = max(range(len(parsed)), key=lambda i: parsed[i]["metascore_count"] or 0)
    return [
        PlatformInfo(
            slug=p["slug"],
            name=p["name"],
            is_lead=(i == lead),  # ровно одна ведущая, даже если API пометил несколько
            release_date=p["release_date"],
            metascore=p["metascore"],
            metascore_count=p["metascore_count"],
            metascore_sentiment=p["metascore_sentiment"],
        )
        for i, p in enumerate(parsed)
    ]


def _bucket(item: dict, name: Bucket) -> list[Quote]:
    raw_list = item.get(name)
    if not isinstance(raw_list, list):
        return []
    quotes = []
    for raw in raw_list:
        text = _as_str(raw.get("quote")) if isinstance(raw, dict) else None
        if not text:
            continue  # отзыв без текста модели бесполезен
        quotes.append(
            Quote(text=text, score=_as_float(raw.get("score")), author=_as_str(raw.get("author")), bucket=name)
        )
    return quotes


class MetacriticClient:
    """Все вызовы к `backend.metacritic.com`, каждый возвращает DTO из `dto.py`.

    Транспорт (rate-limit, retry, ошибки) — в `MetacriticTransport`, здесь только
    построение URL и маппинг. Наружу летит единственное исключение —
    `MetacriticError`.
    """

    def __init__(self, transport: MetacriticTransport | None = None) -> None:
        self._t = transport or MetacriticTransport()

    async def list_new_releases(self, limit: int = 20) -> list[CatalogItem]:
        """Блок New Releases со страницы /game/ — первый заход суток."""
        payload = await self._t.get_json(
            "/finder/metacritic/web",
            {
                "componentName": "new-releases-carousel",
                "componentType": "ProductList",
                "sortBy": "-releaseDate",
                "metaScoreMin": 1,  # без этого фильтра лезут анонсы без единого отзыва
                "mcoTypeId": 13,  # research: 13 = игры
                "offset": 0,
                "limit": limit,
            },
        )
        return [item for item in map(_catalog_item, _items(payload)) if item is not None]

    async def list_browse(self, offset: int, limit: int = 20) -> BrowsePage:
        """SEE ALL, сортировка «новые». Дедуп — по id, не по offset (research RISK #1)."""
        payload = await self._t.get_json(
            "/finder/metacritic/web",
            {"sortBy": "-releaseDate", "mcoTypeId": 13, "offset": offset, "limit": limit},
        )
        items = [item for item in map(_catalog_item, _items(payload)) if item is not None]
        return BrowsePage(items=items, offset=offset, total=_total(payload))

    async def get_product(self, slug: str) -> Product:
        """Карточка игры целиком. Userscore сюда не входит — см. `get_score_stats`."""
        payload = await self._t.get_json(
            f"/games/metacritic/{quote_path(slug)}/web",
            {"componentName": "product", "componentType": "Product"},
        )
        item = _item(payload)
        game_id = _as_int(item.get("id"))
        title = _as_str(item.get("title"))
        if game_id is None or not title:
            raise MetacriticError(200, f"/games/metacritic/{slug}/web", f"product без id/title: {str(item)[:200]}")
        video = item.get("video") if isinstance(item.get("video"), dict) else {}
        genres = item.get("genres") if isinstance(item.get("genres"), list) else []
        return Product(
            id=game_id,
            slug=_as_str(item.get("slug")) or slug,
            title=title,
            description=_as_str(item.get("description")),
            developer=_company(item, "Developer"),
            publisher=_company(item, "Publisher"),
            esrb_rating=_as_str(item.get("rating")),
            release_date=_as_date(item.get("releaseDate")),
            cover_path=_cover_path(item),
            # официальный трейлер Metacritic (JW Player), не летсплей с YouTube
            video_url=_as_str(video.get("embedUrl")) or _as_str(video.get("manifestUrl")),
            genres=[g for g in (_as_str(x.get("name")) for x in genres if isinstance(x, dict)) if g],
            platforms=_platforms(item),
            raw=payload,
        )

    async def get_score_stats(self, slug: str, platform_slug: str, audience: Audience) -> ScoreStats | None:
        """Сводка оценок по платформе. `None`, если оценок для неё нет."""
        try:
            payload = await self._t.get_json(
                f"/reviews/metacritic/{audience}/games/{quote_path(slug)}"
                f"/platform/{quote_path(platform_slug)}/stats/web"
            )
        except MetacriticError as exc:
            if exc.status == 404:
                return None  # платформа есть в product, но отзывов по ней нет
            raise
        item = _item(payload)
        score = _as_float(item.get("score"))
        sentiment = _as_str(item.get("sentiment"))
        if score is None:
            return None
        if score == 0 and sentiment in (None, "tbd"):
            # Живой факт (в research не попал): пока оценка не опубликована,
            # stats-эндпоинт отдаёт score=0 с sentiment=null, а не null.
            # Пример: blood-dungeon/pc — reviewCount=3, positiveCount=2, score=0.
            # Записать это как 0.0 значит соврать в карточке и в сортировке.
            return None
        return ScoreStats(
            score=score,
            count=_as_int(item.get("reviewCount")),
            sentiment=sentiment,
        )

    async def get_review_summary(self, slug: str, platform_slug: str, audience: Audience) -> ReviewQuotes:
        """Готовая подборка цитат по тональности — основной вход LLM-пайплайна."""
        try:
            payload = await self._t.get_json(
                f"/reviews/metacritic/{audience}/games/{quote_path(slug)}"
                f"/platform/{quote_path(platform_slug)}/summary/web"
            )
        except MetacriticError as exc:
            if exc.status == 404:
                return ReviewQuotes()
            raise
        item = _item(payload)
        return ReviewQuotes(
            default=_bucket(item, "default"),
            positive=_bucket(item, "positive"),
            neutral=_bucket(item, "neutral"),
            negative=_bucket(item, "negative"),
        )

    async def list_reviews(
        self,
        slug: str,
        platform_slug: str,
        audience: Audience,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Review]:
        """Полный список отзывов — fallback, когда summary пуст.

        У критиков размер страницы жёстко 10, `limit` сервер игнорирует
        (research: `critic_reviews_list.pagination`).
        """
        payload = await self._t.get_json(
            f"/reviews/metacritic/{audience}/games/{quote_path(slug)}/platform/{quote_path(platform_slug)}/web",
            {
                "offset": offset,
                "limit": limit,
                "filterBySentiment": "all",
                "sort": "date",
                "componentType": "ReviewList",
            },
        )
        reviews: list[Review] = []
        for raw in _items(payload):
            text = _as_str(raw.get("quote"))
            if not text:
                continue
            reviews.append(
                Review(
                    text=text,
                    score=_as_float(raw.get("score")),
                    author=_as_str(raw.get("author")),
                    publication=_as_str(raw.get("publicationName")),
                    date=_as_date(raw.get("date")),
                )
            )
        return reviews

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> MetacriticClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
