"""T-08: запись игры в БД. Тесты идут в реальный Postgres из DATABASE_URL.

Мока для БД нет намеренно: проверяется как раз то, что мок и не воспроизведёт —
ON CONFLICT, согласованность `genres_cache` с `game_genres` и атомарность.
Игры берут заведомо синтетические id, чтобы не задеть реальные данные.
"""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import PlatformInfo, Product, ScoreStats
from app.db import dispose_engine, get_engine
from app.ingest.upsert import upsert_game

TEST_ID = 9_000_000_001


@pytest.fixture(autouse=True)
async def clean_test_rows():
    """Чистый стол до и после теста.

    Движок в конце разбирается: pytest-asyncio даёт каждому тесту свой event
    loop, а соединение из пула привязано к тому, в котором открыто, — иначе
    следующий тест получит «Future attached to a different loop».
    """
    try:
        await delete_test_rows()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    try:
        yield
        await delete_test_rows()
    finally:
        await dispose_engine()


async def delete_test_rows() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM games WHERE id >= 9000000000"))


def make_product(**overrides) -> Product:
    base = dict(
        id=TEST_ID,
        slug="test-game",
        title="Test Game",
        description="Описание",
        developer="Dev Co",
        publisher="Pub Co",
        esrb_rating="M",
        release_date=date(2026, 9, 4),
        cover_path="/provider/7/2/x.jpg",
        video_url="https://cdn.jwplayer.com/players/X.html",
        genres=["Action", "Adventure"],
        platforms=[
            PlatformInfo("pc", "PC", is_lead=False, metascore=70, metascore_count=4, metascore_sentiment="Mixed"),
            PlatformInfo("ps5", "PS5", is_lead=True, metascore=88, metascore_count=30, metascore_sentiment="Good"),
        ],
        raw={"data": {"item": {"id": TEST_ID}}},
    )
    base.update(overrides)
    return Product(**base)


async def fetch_game(game_id: int = TEST_ID) -> dict | None:
    async with get_engine().connect() as conn:
        row = (await conn.execute(text("SELECT * FROM games WHERE id = :id"), {"id": game_id})).mappings().first()
    return dict(row) if row else None


async def fetch_platforms(game_id: int = TEST_ID) -> list[dict]:
    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT * FROM game_platforms WHERE game_id = :id ORDER BY platform_slug"), {"id": game_id}
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def fetch_genres(game_id: int = TEST_ID) -> list[str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT genre FROM game_genres WHERE game_id = :id ORDER BY genre"), {"id": game_id}
        )
    return [r[0] for r in rows]


async def test_upsert_writes_game_platforms_and_genres():
    user_scores = {"pc": ScoreStats(6.4, 12, "Mixed"), "ps5": ScoreStats(9.1, 40, "Universal acclaim")}
    game_id = await upsert_game(make_product(), user_scores)
    assert game_id == TEST_ID

    game = await fetch_game()
    assert game["title"] == "Test Game"
    assert game["description"] == "Описание"
    assert game["developer"] == "Dev Co" and game["publisher"] == "Pub Co"
    assert game["esrb_rating"] == "M"
    assert game["release_date"] == date(2026, 9, 4)
    assert game["cover_path"] == "/provider/7/2/x.jpg"
    assert game["video_url"].endswith("X.html")
    assert game["lead_platform_slug"] == "ps5"
    assert game["best_metascore"] == 88
    assert float(game["best_userscore"]) == 9.1
    assert game["raw"]["data"]["item"]["id"] == TEST_ID

    platforms = await fetch_platforms()
    assert [p["platform_slug"] for p in platforms] == ["pc", "ps5"]
    assert [p["is_lead"] for p in platforms] == [False, True]
    assert platforms[1]["metascore"] == 88 and platforms[1]["metascore_count"] == 30
    assert float(platforms[0]["userscore"]) == 6.4 and platforms[0]["userscore_count"] == 12
    assert platforms[1]["userscore_sentiment"] == "Universal acclaim"


async def test_genres_cache_matches_game_genres():
    await upsert_game(make_product(genres=["Action", "Adventure", "Action"]))
    game = await fetch_game()
    assert game["genres_cache"] == ["Action", "Adventure"]  # дубли схлопнуты
    assert sorted(game["genres_cache"]) == await fetch_genres()


async def test_second_upsert_updates_and_does_not_duplicate():
    await upsert_game(make_product(), {"ps5": ScoreStats(9.1, 40, "acclaim")})
    first = await fetch_game()

    await upsert_game(
        make_product(title="Test Game GOTY", genres=["Action", "RPG"]),
        {"ps5": ScoreStats(9.4, 55, "acclaim")},
    )
    second = await fetch_game()

    assert second["title"] == "Test Game GOTY"
    assert second["updated_at"] > first["updated_at"]
    assert second["first_seen_at"] == first["first_seen_at"]  # дата первого появления не переписывается
    assert float(second["best_userscore"]) == 9.4
    assert len(await fetch_platforms()) == 2  # платформы не задвоились
    assert await fetch_genres() == ["Action", "RPG"]  # исчезнувший жанр убран
    assert (await fetch_game())["genres_cache"] == ["Action", "RPG"]


async def test_platform_that_disappeared_from_api_is_removed():
    await upsert_game(make_product())
    only_ps5 = [PlatformInfo("ps5", "PS5", is_lead=True, metascore=88, metascore_count=30)]
    await upsert_game(make_product(platforms=only_ps5))
    assert [p["platform_slug"] for p in await fetch_platforms()] == ["ps5"]


async def test_best_scores_are_null_when_nothing_is_scored():
    platforms = [PlatformInfo("pc", "PC", is_lead=True)]
    await upsert_game(make_product(platforms=platforms), {"pc": None})
    game = await fetch_game()
    assert game["best_metascore"] is None
    assert game["best_userscore"] is None
    assert game["lead_platform_slug"] == "pc"


async def test_game_without_platforms_or_genres_is_still_written():
    await upsert_game(make_product(platforms=[], genres=[]))
    game = await fetch_game()
    assert game is not None
    assert game["genres_cache"] == []
    assert game["lead_platform_slug"] is None
    assert await fetch_platforms() == []


async def test_failure_inside_transaction_leaves_no_partial_game():
    # smallint не вместит 999999 — падаем на вставке платформы, уже после games
    broken = [PlatformInfo("pc", "PC", is_lead=True, metascore=999999)]
    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await upsert_game(make_product(platforms=broken))
    assert await fetch_game() is None, "games не должна остаться без своих платформ"
    assert await fetch_platforms() == []
