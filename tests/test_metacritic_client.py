"""T-06: методы клиента Metacritic — маппинг реальных ответов API в DTO.

Фикстуры в `tests/fixtures/` сняты с живого API (см. docs/00-research.json),
не написаны руками: тест проверяет маппинг ровно той формы, которую отдаёт
Metacritic, а не наши представления о ней.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.clients import metacritic as mc
from app.clients.metacritic import MetacriticClient
from tests.test_metacritic_transport import make_settings

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def client_serving(route_to_fixture: dict[str, str] | None = None, *, payload: dict | None = None):
    """Клиент поверх MockTransport: путь запроса → имя фикстуры."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if payload is not None:
            return httpx.Response(200, json=payload)
        for fragment, fixture in (route_to_fixture or {}).items():
            if fragment in str(request.url):
                return httpx.Response(200, json=load(fixture))
        return httpx.Response(404, text="no fixture for this route")

    settings = make_settings(metacritic_rps=1000)
    http = httpx.AsyncClient(base_url=settings.metacritic_base_url, transport=httpx.MockTransport(handler))
    return MetacriticClient(mc.MetacriticTransport(settings, client=http)), seen


# --- каталог ---------------------------------------------------------------


async def test_new_releases_maps_items_and_sends_game_filters():
    client, seen = client_serving({"finder": "new_releases"})
    items = await client.list_new_releases(limit=5)

    assert len(items) == 5
    first = items[0]
    assert first.id == 1300662655
    assert first.slug == "onimusha-way-of-the-sword"
    assert first.title == "Onimusha: Way of the Sword"
    assert first.release_date == date(2026, 9, 4)

    params = seen[0].url.params
    assert params["componentName"] == "new-releases-carousel"
    assert params["mcoTypeId"] == "13"  # research: 13 = игры
    assert params["limit"] == "5"
    await client.aclose()


async def test_browse_returns_offset_and_total():
    client, seen = client_serving({"finder": "browse_20"})
    page = await client.list_browse(offset=20, limit=5)

    assert page.offset == 20
    assert page.total == 177895
    assert len(page.items) == 5
    assert seen[0].url.params["offset"] == "20"
    await client.aclose()


async def test_catalog_item_without_id_or_slug_is_skipped():
    payload = {
        "data": {
            "totalResults": 3,
            "items": [
                {"id": 1, "slug": "ok", "title": "Ok"},
                {"id": None, "slug": "no-id", "title": "No id"},
                {"id": 2, "slug": None, "title": "No slug"},
            ],
        }
    }
    client, _ = client_serving(payload=payload)
    page = await client.list_browse(offset=0)
    assert [i.slug for i in page.items] == ["ok"]
    await client.aclose()


# --- product ---------------------------------------------------------------


async def test_product_maps_every_field_of_the_card():
    client, seen = client_serving({"/games/metacritic/": "product"})
    product = await client.get_product("onimusha-way-of-the-sword")

    assert product.id == 1300662655
    assert product.title == "Onimusha: Way of the Sword"
    assert product.developer == "Capcom"
    assert product.publisher == "Capcom"
    assert product.esrb_rating == "M"
    assert product.release_date == date(2026, 9, 4)
    assert product.genres == ["Action Adventure"]
    assert product.description and "Onimusha" not in product.description[:1]
    # cardImage (постер 226x332) предпочтительнее mainImage (баннер 1200x630)
    assert product.cover_path == "/provider/7/2/7-1781631535.jpg"
    assert product.video_url == "https://cdn.jwplayer.com/players/Eibtv31Y.html"
    assert product.raw["data"]["item"]["id"] == 1300662655  # сырой ответ целиком, для games.raw
    assert seen[0].url.params["componentName"] == "product"
    await client.aclose()


async def test_product_drops_a_video_url_with_an_untrusted_scheme():
    """`video_url` уходит в `<iframe src>` карточки: там `javascript:` исполнится.

    Проверка стоит на границе с чужим API, чтобы такая строка не доехала до БД.
    """
    payload = load("product")
    payload["data"]["item"]["video"] = {
        "embedUrl": "javascript:alert(document.domain)",
        "manifestUrl": "javascript:alert(1)",
    }
    client, _ = client_serving(payload=payload)

    product = await client.get_product("onimusha-way-of-the-sword")

    assert product.video_url is None
    await client.aclose()


async def test_product_falls_back_to_the_manifest_url_when_embed_is_unusable():
    payload = load("product")
    payload["data"]["item"]["video"] = {
        "embedUrl": "javascript:alert(1)",
        "manifestUrl": "https://cdn.jwplayer.com/manifests/X.m3u8",
    }
    client, _ = client_serving(payload=payload)

    product = await client.get_product("onimusha-way-of-the-sword")

    assert product.video_url == "https://cdn.jwplayer.com/manifests/X.m3u8"
    await client.aclose()


async def test_product_platforms_carry_metascore_and_single_lead():
    client, _ = client_serving({"/games/metacritic/": "product"})
    product = await client.get_product("onimusha-way-of-the-sword")

    assert [p.slug for p in product.platforms] == ["playstation-5", "pc", "xbox-series-x", "nintendo-switch-2"]
    assert sum(p.is_lead for p in product.platforms) == 1
    lead = product.lead_platform
    assert lead is not None and lead.slug == "playstation-5"
    assert lead.metascore == 85
    assert lead.metascore_count == 92
    assert lead.metascore_sentiment == "Generally favorable"
    await client.aclose()


async def test_lead_platform_falls_back_to_largest_metascore_count():
    """design §8: нет isLeadPlatform — ведущей считаем платформу с бОльшим счётчиком."""
    payload = {
        "data": {
            "item": {
                "id": 7,
                "slug": "x",
                "title": "X",
                "platforms": [
                    {"slug": "pc", "name": "PC", "criticScoreSummary": {"score": 70, "reviewCount": 4}},
                    {"slug": "ps5", "name": "PS5", "criticScoreSummary": {"score": 90, "reviewCount": 11}},
                ],
            }
        }
    }
    client, _ = client_serving(payload=payload)
    product = await client.get_product("x")
    assert product.lead_platform.slug == "ps5"
    await client.aclose()


async def test_lead_platform_tie_breaks_to_first_in_array():
    payload = {
        "data": {
            "item": {
                "id": 7,
                "slug": "x",
                "title": "X",
                "platforms": [
                    {"slug": "pc", "name": "PC"},
                    {"slug": "ps5", "name": "PS5"},
                ],
            }
        }
    }
    client, _ = client_serving(payload=payload)
    product = await client.get_product("x")
    assert product.lead_platform.slug == "pc"
    await client.aclose()


async def test_missing_optional_fields_do_not_break_mapping():
    payload = {"data": {"item": {"id": 7, "slug": "bare", "title": "Bare"}}}
    client, _ = client_serving(payload=payload)
    product = await client.get_product("bare")

    assert product.esrb_rating is None
    assert product.description is None
    assert product.developer is None
    assert product.cover_path is None
    assert product.video_url is None
    assert product.release_date is None
    assert product.genres == []
    assert product.platforms == []
    assert product.lead_platform is None
    await client.aclose()


async def test_product_without_id_is_a_metacritic_error():
    client, _ = client_serving(payload={"data": {"item": {"slug": "x"}}})
    with pytest.raises(mc.MetacriticError):
        await client.get_product("x")
    await client.aclose()


# --- оценки ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("audience", "fixture", "score", "count"),
    [("user", "stats_user", 8.6, 128), ("critic", "stats_critic", 85.0, 89)],
)
async def test_score_stats_per_platform(audience, fixture, score, count):
    client, seen = client_serving({"/stats/web": fixture})
    stats = await client.get_score_stats("onimusha-way-of-the-sword", "playstation-5", audience)

    assert stats is not None
    assert stats.score == score
    assert stats.count == count
    assert stats.sentiment == "Generally favorable"
    # платформа обязана быть сегментом пути: ?platform= сервер игнорирует
    assert f"/{audience}/games/onimusha-way-of-the-sword/platform/playstation-5/stats/web" in str(seen[0].url)
    await client.aclose()


async def test_score_stats_is_none_when_platform_has_no_scores():
    client, _ = client_serving({})  # любая ссылка → 404
    assert await client.get_score_stats("x", "sega-saturn", "user") is None
    await client.aclose()


async def test_unpublished_zero_score_is_treated_as_absent():
    """API отдаёт score=0/sentiment=null, пока оценка не опубликована (живой факт)."""
    payload = {"data": {"item": {"max": 10, "score": 0, "reviewCount": 3, "sentiment": None}}}
    client, _ = client_serving(payload=payload)
    assert await client.get_score_stats("blood-dungeon", "pc", "user") is None
    await client.aclose()


async def test_real_zero_score_with_sentiment_is_kept():
    payload = {"data": {"item": {"max": 100, "score": 0, "reviewCount": 9, "sentiment": "Overwhelming dislike"}}}
    client, _ = client_serving(payload=payload)
    stats = await client.get_score_stats("x", "pc", "critic")
    assert stats is not None and stats.score == 0.0
    await client.aclose()


async def test_score_stats_is_none_when_score_is_null():
    client, _ = client_serving(payload={"data": {"item": {"max": 10, "score": None, "reviewCount": 0}}})
    assert await client.get_score_stats("x", "pc", "user") is None
    await client.aclose()


# --- отзывы ----------------------------------------------------------------


async def test_review_summary_splits_quotes_by_sentiment():
    client, seen = client_serving({"/summary/web": "summary_user"})
    quotes = await client.get_review_summary("onimusha-way-of-the-sword", "playstation-5", "user")

    assert not quotes.is_empty()
    assert len(quotes.default) == 7
    assert len(quotes.negative) == 5
    assert all(q.bucket == "negative" for q in quotes.negative)
    assert all(q.text for q in quotes.positive)
    assert quotes.default[0].author == "hedcamargo"
    assert "/summary/web" in str(seen[0].url)
    await client.aclose()


async def test_empty_summary_is_empty_not_an_error():
    """Живой факт: у критиков по ведущей платформе подборка бывает пустой — отсюда fallback (T-30)."""
    client, _ = client_serving({"/summary/web": "summary_critic"})
    quotes = await client.get_review_summary("onimusha-way-of-the-sword", "playstation-5", "critic")
    assert quotes.is_empty()
    await client.aclose()


async def test_review_list_maps_text_score_and_publication():
    client, seen = client_serving({"/platform/playstation-5/web": "reviews_critic"})
    reviews = await client.list_reviews("onimusha-way-of-the-sword", "playstation-5", "critic", limit=5)

    assert reviews
    assert reviews[0].publication == "Reloader"
    assert reviews[0].score == 80.0
    assert reviews[0].date == date(2026, 9, 4)
    assert reviews[0].to_quote("positive").bucket == "positive"
    params = seen[0].url.params
    assert params["filterBySentiment"] == "all" and params["componentType"] == "ReviewList"
    await client.aclose()


async def test_reviews_without_text_are_dropped():
    payload = {"data": {"totalResults": 2, "items": [{"quote": "", "score": 3}, {"quote": "есть текст", "score": 9}]}}
    client, _ = client_serving(payload=payload)
    reviews = await client.list_reviews("x", "pc", "user")
    assert [r.text for r in reviews] == ["есть текст"]
    await client.aclose()


# --- обложка ---------------------------------------------------------------


async def test_cover_url_is_reachable_shape():
    """URL обложки строится как {cdn}/{bucketPath}, где cdn уже содержит bucketType.

    Отдельный тест, потому что документированный в research путь без bucketType
    отдаёт 404 — регрессия здесь ломает картинки на всём списке.
    """
    from app.web.templating import cover_url

    client, _ = client_serving({"/games/metacritic/": "product"})
    product = await client.get_product("onimusha-way-of-the-sword")
    assert cover_url(product.cover_path) == (
        "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1781631535.jpg"
    )
    await client.aclose()
