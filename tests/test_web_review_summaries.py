"""T-31: блоки резюме отзывов в карточке игры.

БД настоящая (как в остальных тестах витрины), страница рендерится целиком
через ASGI. Проверяется главное свойство блока: у него три внятных состояния —
готовое резюме, «отзывов нет» и «ещё не готово», — и ни в одном из них на
экран не попадает техническая ошибка.
"""

from __future__ import annotations

import json

import httpx
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import PlatformInfo, Product
from app.db import dispose_engine, get_engine
from app.ingest.upsert import upsert_game
from app.main import app
from app.web.repo_games import get_review_summaries

GAME_ID = 9_000_600_001
SLUG = "zzq-card-1"
PLATFORM = "test-plat"

LIKED = ["плотный дизайн уровней", "музыка"]
DISLIKED = ["просадки кадров"]
TLDR = "Приняли тепло."

_INSERT = text(
    """
    INSERT INTO review_summaries (
        game_id, audience, platform_slug, liked, disliked, tldr,
        quotes_count, quotes_hash, source, status, error
    ) VALUES (
        :game_id, :audience, :platform, CAST(:liked AS jsonb), CAST(:disliked AS jsonb),
        :tldr, :count, :hash, :source, :status, :error
    )
    """
)


@pytest.fixture(autouse=True)
async def game_row():
    try:
        await _delete_test_rows()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    await upsert_game(
        Product(
            id=GAME_ID, slug=SLUG, title="ZZQ Card", raw={},
            platforms=[PlatformInfo(slug=PLATFORM, name="TEST", is_lead=True, metascore=80)],
        ),
        {},
    )
    try:
        yield
        await _delete_test_rows()
    finally:
        await dispose_engine()


async def _delete_test_rows() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM games WHERE id >= 9000000000"))


async def add_summary(audience: str, status: str = "ok", **overrides) -> None:
    params = {
        "game_id": GAME_ID, "audience": audience, "platform": PLATFORM,
        "liked": json.dumps(LIKED if status == "ok" else [], ensure_ascii=False),
        "disliked": json.dumps(DISLIKED if status == "ok" else [], ensure_ascii=False),
        "tldr": TLDR if status == "ok" else None,
        "count": 12, "hash": "h" * 64, "source": "summary_endpoint",
        "status": status, "error": None,
    } | overrides
    async with get_engine().begin() as conn:
        await conn.execute(_INSERT, params)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def card() -> str:
    async with client() as http:
        response = await http.get(f"/game/{SLUG}")
    assert response.status_code == 200
    return response.text


async def test_repo_returns_summaries_keyed_by_audience():
    await add_summary("critic")
    await add_summary("user", status="no_data")

    rows = await get_review_summaries(GAME_ID)

    assert set(rows) == {"critic", "user"}
    assert rows["critic"]["liked"] == LIKED  # jsonb приезжает списком, а не строкой
    assert rows["user"]["status"] == "no_data"
    assert await get_review_summaries(GAME_ID + 999) == {}


async def test_ok_summaries_render_two_separate_blocks():
    await add_summary("critic")
    await add_summary("user")

    html = await card()

    assert html.count('class="summary"') == 2
    assert 'data-audience="critic"' in html and 'data-audience="user"' in html
    assert html.index('data-audience="critic"') < html.index('data-audience="user"')
    for point in LIKED + DISLIKED:
        assert point in html
    assert TLDR in html
    assert "Нравится" in html and "Не нравится" in html


async def test_no_data_says_there_are_no_reviews_yet():
    await add_summary("critic", status="no_data")
    await add_summary("user", status="no_data")

    html = await card()

    assert html.count("Отзывов пока нет.") == 2
    assert "Резюме не готово" not in html


async def test_llm_failed_shows_neutral_text_without_the_error():
    await add_summary("critic", status="llm_failed", error="429 RESOURCE_EXHAUSTED")

    html = await card()

    assert "Резюме не готово, обновится при следующем обходе." in html
    assert "429" not in html and "RESOURCE_EXHAUSTED" not in html


async def test_game_without_summaries_shows_pending_not_an_empty_block():
    """Игра только что заклеймлена, пайплайн ещё не отработал."""
    html = await card()

    assert html.count("Резюме не готово, обновится при следующем обходе.") == 2
    assert 'id="review-summaries"' in html
    assert "Критики" in html and "Пользователи" in html


async def test_summary_text_is_escaped():
    """Резюме собрано из недоверенного ввода — в HTML оно тоже данные."""
    await add_summary("user", liked=json.dumps(["<script>alert(1)</script>"]))

    html = await card()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
