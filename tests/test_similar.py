"""T-33/T-34: похожие игры — ранжирование и блок в карточке.

БД настоящая, как в остальных тестах витрины: проверяется ровно то, чего мок не
покажет, — порядок из ORDER BY, вес разработчика и поведение на игре без жанров.
Игры синтетические, id из служебного диапазона.
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
from app.similar import list_similar

BASE_ID = 9_000_700_000
PLATFORM = "test-plat"

# Опорная игра: три жанра, студия «Dev Us».
#
# Ожидаемый порядок для неё (score = общие жанры + 2 за студию):
#   twin   3 жанра  + студия -> 5
#   triple 3 жанра           -> 3
#   studio 1 жанр   + студия -> 3, но Metascore ниже, чем у triple
#   double 2 жанра           -> 2
#   single 1 жанр            -> 1
#   alien  0 общих жанров    -> в выдачу не попадает вовсе
FIXTURE = [
    (1, "Anchor", ["Action", "RPG", "Indie"], "Dev Us", 70),
    (2, "Twin", ["Action", "RPG", "Indie"], "Dev Us", 60),
    (3, "Triple", ["Action", "RPG", "Indie"], "Dev Them", 90),
    (4, "Studio", ["Action"], "Dev Us", 50),
    (5, "Double", ["Action", "RPG"], "Dev Them", 80),
    (6, "Single", ["Action"], "Dev Them", 85),
    (7, "Alien", ["Puzzle"], "Dev Them", 95),
    (8, "Bare", [], "Dev Us", 65),
]
ANCHOR_ID, BARE_ID = BASE_ID + 1, BASE_ID + 8


def product(n: int, title: str, genres: list[str], developer: str, metascore: int) -> Product:
    return Product(
        id=BASE_ID + n,
        slug=f"zzqsim-{n}",
        title=title,
        developer=developer,
        cover_path="/provider/7/2/x.jpg",
        genres=genres,
        platforms=[PlatformInfo(slug=PLATFORM, name="TEST", is_lead=True, metascore=metascore)],
        raw={"n": n},
    )


@pytest.fixture(autouse=True)
async def games_fixture():
    """Чистый стол до и после теста; движок разбирается — см. tests/test_upsert.py."""
    try:
        await _delete_test_rows()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    for args in FIXTURE:
        await upsert_game(product(*args), {})
    try:
        yield
        await _delete_test_rows()
    finally:
        await dispose_engine()


async def _delete_test_rows() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM games WHERE id >= 9000000000"))


async def card(slug: str) -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get(f"/game/{slug}")
    assert response.status_code == 200
    return response.text


async def test_ranking_counts_shared_genres_and_weighs_the_studio():
    rows = await list_similar(ANCHOR_ID)

    assert [row["title"] for row in rows] == ["Twin", "Triple", "Studio", "Double", "Single"]
    assert [row["score"] for row in rows] == [5, 3, 3, 2, 1]
    # У Studio один общий жанр, наверх её поднимают именно +2 за совпадение студии.
    assert next(row for row in rows if row["title"] == "Studio")["shared_genres"] == 1


async def test_returns_at_most_five_and_never_the_game_itself():
    rows = await list_similar(ANCHOR_ID)

    assert len(rows) == 5
    assert ANCHOR_ID not in {row["id"] for row in rows}
    assert "Alien" not in {row["title"] for row in rows}  # ни одного общего жанра


async def test_game_without_genres_gets_an_empty_result():
    assert await list_similar(BARE_ID) == []


async def test_missing_game_gets_an_empty_result_not_an_error():
    assert await list_similar(BASE_ID + 999) == []


async def test_card_renders_the_block_with_links_to_existing_cards():
    html = await card("zzqsim-1")

    assert 'id="similar-games"' in html and "Похожие игры" in html
    assert html.count('class="title" href="/game/zzqsim-') == 5
    assert 'href="/game/zzqsim-2"' in html          # Twin — самая близкая
    assert 'href="/game/zzqsim-7"' not in html      # Alien — не похожая
    assert 'href="/game/zzqsim-1"' not in html      # ссылки на саму себя нет


async def test_card_without_similar_games_hides_the_block():
    html = await card("zzqsim-8")

    assert 'id="similar-games"' not in html
    assert "Похожие игры" not in html
