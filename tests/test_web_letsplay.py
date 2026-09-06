"""T-44: блок «Летсплей» в карточке игры.

БД настоящая, страница рендерится целиком через ASGI (как в остальных тестах
витрины). Главное свойство блока: он появляется только тогда, когда есть что
показать, а `not_found`/`service_error` не превращаются в пустую секцию —
фича best-effort, и карточка без неё выглядит цельной (ADR-8).
"""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import PlatformInfo, Product
from app.db import dispose_engine, get_engine
from app.ingest.upsert import upsert_game
from app.main import app
from app.web.repo_games import get_letsplay

GAME_ID = 9_000_800_001
SLUG = "zzq-letsplay-card"
VIDEO_URL = "https://www.youtube.com/watch?v=vid1"
CONCLUSION = "Бодрый экшен с внятной боевой системой."
RETELLING = "Блогер проходит игру и хвалит боевую систему."

_INSERT = text(
    """
    INSERT INTO letsplays (
        game_id, status, video_id, video_url, video_title, channel, view_count,
        retelling, conclusion, attempts, error, last_attempt_at
    ) VALUES (
        :game_id, :status, :video_id, :video_url, :video_title, :channel, :view_count,
        :retelling, :conclusion, 1, :error, now()
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
            id=GAME_ID, slug=SLUG, title="ZZQ Letsplay Card", raw={},
            platforms=[PlatformInfo(slug="test-plat", name="TEST", is_lead=True, metascore=80)],
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


async def add_letsplay(status: str = "ok", **overrides) -> None:
    params = {
        "game_id": GAME_ID, "status": status,
        "video_id": "vid1", "video_url": VIDEO_URL,
        "video_title": "ZZQ — полное прохождение", "channel": "ZZQ Plays",
        "view_count": 421_000,
        "retelling": RETELLING if status == "ok" else None,
        "conclusion": CONCLUSION if status == "ok" else None,
        "error": None,
    } | overrides
    async with get_engine().begin() as conn:
        await conn.execute(_INSERT, params)


async def card() -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get(f"/game/{SLUG}")
    assert response.status_code == 200
    return response.text


async def test_repo_returns_none_for_a_game_without_letsplay():
    assert await get_letsplay(GAME_ID) is None
    await add_letsplay()
    assert (await get_letsplay(GAME_ID))["video_url"] == VIDEO_URL


async def test_ok_letsplay_shows_conclusion_and_a_link_to_the_video():
    await add_letsplay()

    html = await card()

    assert 'id="letsplay"' in html
    assert CONCLUSION in html
    assert f'href="{VIDEO_URL}"' in html
    assert "ZZQ Plays" in html
    assert "421" in html  # просмотры с разделителем разрядов
    assert RETELLING in html


async def test_service_error_does_not_render_an_empty_block():
    await add_letsplay(status="service_error", error="auth: 403 forbidden")

    html = await card()

    assert 'id="letsplay"' not in html
    assert "403" not in html and "auth" not in html


async def test_not_found_does_not_render_the_block():
    await add_letsplay(status="not_found", video_id=None, video_url=None,
                       video_title=None, channel=None, view_count=None)

    html = await card()

    assert 'id="letsplay"' not in html


async def test_game_without_letsplay_row_renders_the_rest_of_the_card():
    html = await card()

    assert 'id="letsplay"' not in html
    assert 'id="review-summaries"' in html  # остальная карточка на месте


async def test_conclusion_is_escaped():
    """Заключение собрано моделью по недоверенному пересказу — в HTML это данные."""
    await add_letsplay(conclusion="<script>alert(1)</script>")

    html = await card()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
