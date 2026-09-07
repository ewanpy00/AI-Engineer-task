"""T-18: курсор дня и журнал claim'ов. Тесты идут в реальный Postgres.

Мока для БД нет намеренно: проверяется ровно то, чего мок не покажет —
`ON CONFLICT DO NOTHING` при гонке двух заходов, `RETURNING` как источник
списка «кого обрабатывать» и то, что упавшая игра не переклеймится сегодня.
День берётся синтетический, из далёкого будущего, чтобы не задеть реальные
строки за сегодня.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import CatalogItem
from app.db import dispose_engine, get_engine
from app.ingest.day_cursor_repo import DayCursorRepo
from app.ingest.processed_repo import RECLAIM_LIMIT, ProcessedRepo

TEST_DAY = date(2999, 1, 1)
BASE_ID = 9_000_200_000


def items(count: int, start: int = 0) -> list[CatalogItem]:
    return [
        CatalogItem(id=BASE_ID + i, slug=f"zzq-{i}", title=f"ZZQ {i}")
        for i in range(start, start + count)
    ]


@pytest.fixture(autouse=True)
async def clean_test_rows():
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
        await conn.execute(text("DELETE FROM day_cursor WHERE day = :day"), {"day": TEST_DAY})
        await conn.execute(
            text("DELETE FROM processed_games WHERE day = :day"), {"day": TEST_DAY}
        )


async def test_get_or_create_starts_day_with_new_releases():
    cursor = await DayCursorRepo().get_or_create(TEST_DAY)
    assert (cursor.day, cursor.phase, cursor.browse_offset) == (TEST_DAY, "new_releases", 0)
    assert (cursor.runs_count, cursor.claimed_count) == (0, 0)


async def test_get_or_create_is_idempotent_under_race():
    """Два захода стартовали одновременно — курсор всё равно один (ADR-6)."""
    repo = DayCursorRepo()
    first, second = await asyncio.gather(
        repo.get_or_create(TEST_DAY), repo.get_or_create(TEST_DAY)
    )
    assert first == second
    async with get_engine().connect() as conn:
        rows = await conn.scalar(
            text("SELECT count(*) FROM day_cursor WHERE day = :day"), {"day": TEST_DAY}
        )
    assert rows == 1


async def test_advance_moves_phase_and_counts_run():
    repo = DayCursorRepo()
    await repo.get_or_create(TEST_DAY)

    after_first = await repo.advance(TEST_DAY, phase="browse", browse_offset=0, claimed=20)
    after_second = await repo.advance(TEST_DAY, phase="browse", browse_offset=20, claimed=13)

    assert (after_first.phase, after_first.browse_offset) == ("browse", 0)
    assert (after_second.browse_offset, after_second.runs_count) == (20, 2)
    assert after_second.claimed_count == 33  # 20 + 13, а не последнее значение


async def test_advance_rejects_unknown_phase():
    await DayCursorRepo().get_or_create(TEST_DAY)
    with pytest.raises(ValueError):
        await DayCursorRepo().advance(TEST_DAY, phase="finished", browse_offset=0, claimed=0)


async def test_claim_returns_only_untouched_games():
    """New Releases ⊂ SEE ALL: пересечение отсекается одним INSERT'ом."""
    repo = ProcessedRepo()
    already = await repo.claim(TEST_DAY, 1, "new_releases", items(5))
    assert len(already) == 5

    fresh = await repo.claim(TEST_DAY, 2, "browse", items(20))
    assert len(fresh) == 15
    assert {item.id for item in fresh}.isdisjoint({item.id for item in already})


async def test_claim_dedups_inside_one_page():
    page = items(3) + items(3)  # один и тот же id дважды в выдаче API
    assert len(await ProcessedRepo().claim(TEST_DAY, 1, "browse", page)) == 3


async def test_failed_game_is_not_reclaimed_the_same_day():
    """«Ядовитая» игра не должна выедать квоту в 20 игр каждый час (ADR-6)."""
    repo = ProcessedRepo()
    [game] = await repo.claim(TEST_DAY, 1, "browse", items(1))
    await repo.finish(TEST_DAY, game.id, ok=False, error="MetacriticError: 500")

    assert await repo.claim(TEST_DAY, 2, "browse", items(1)) == []
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, error, finished_at FROM processed_games WHERE game_id = :id"),
                {"id": game.id},
            )
        ).one()
    assert row.status == "failed"
    assert row.error == "MetacriticError: 500"
    assert row.finished_at is not None


async def status_row(game_id: int):
    async with get_engine().connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT status, source, run_id, error, finished_at "
                    "FROM processed_games WHERE day = :day AND game_id = :id"
                ),
                {"day": TEST_DAY, "id": game_id},
            )
        ).one()


async def test_reclaim_returns_failed_games_to_work():
    """OQ-7: кнопка забирает упавшие сегодня игры, плановый заход — по-прежнему нет."""
    repo = ProcessedRepo()
    claimed = await repo.claim(TEST_DAY, 1, "browse", items(3))
    await repo.finish(TEST_DAY, claimed[0].id, ok=False, error="MetacriticError: 500")
    await repo.finish(TEST_DAY, claimed[1].id, ok=True)

    back = await repo.reclaim_failed(TEST_DAY, 7)

    assert [game.id for game in back] == [claimed[0].id]
    # названия в `games` нет: игра могла упасть на первом же обращении к
    # Metacritic, до вставки в каталог, — тогда вместо названия идёт slug
    assert back[0].title == claimed[0].slug

    row = await status_row(claimed[0].id)
    assert (row.status, row.source, row.run_id) == ("claimed", "manual", 7)
    assert (row.error, row.finished_at) == (None, None)
    assert (await repo.counters(TEST_DAY)).failed == 0

    # игра снова в работе, а не свободна: ни плановый заход, ни второе нажатие
    # её сегодня не возьмут
    assert await repo.claim(TEST_DAY, 8, "browse", items(3)) == []
    assert await repo.reclaim_failed(TEST_DAY, 9) == []
    # успешную игру переклейм не трогает
    assert (await status_row(claimed[1].id)).status == "ok"


async def test_reclaim_takes_no_more_than_the_quota_per_run():
    """OQ-7: максимум 20 игр за нажатие — лежавший полдня Metacritic не должен
    превращать один заход в многочасовой."""
    repo = ProcessedRepo()
    claimed = await repo.claim(TEST_DAY, 1, "browse", items(RECLAIM_LIMIT + 5))
    for game in claimed:
        await repo.finish(TEST_DAY, game.id, ok=False, error="boom")

    first = await repo.reclaim_failed(TEST_DAY, 2)
    second = await repo.reclaim_failed(TEST_DAY, 3)  # остаток ждёт следующего нажатия

    assert (len(first), len(second)) == (RECLAIM_LIMIT, 5)
    assert {game.id for game in first}.isdisjoint({game.id for game in second})


async def test_reclaim_does_not_touch_the_day_cursor():
    """Переклейм идёт сверх дневной квоты: фаза и offset дня остаются на месте."""
    repo = ProcessedRepo()
    await DayCursorRepo().get_or_create(TEST_DAY)
    before = await DayCursorRepo().advance(
        TEST_DAY, phase="browse", browse_offset=40, claimed=20
    )
    [game] = await repo.claim(TEST_DAY, 1, "browse", items(1))
    await repo.finish(TEST_DAY, game.id, ok=False, error="boom")

    assert len(await repo.reclaim_failed(TEST_DAY, 2)) == 1

    assert await DayCursorRepo().get(TEST_DAY) == before


async def test_counters_match_table():
    repo = ProcessedRepo()
    claimed = await repo.claim(TEST_DAY, 1, "browse", items(4))
    await repo.finish(TEST_DAY, claimed[0].id, ok=True)
    await repo.finish(TEST_DAY, claimed[1].id, ok=True)
    await repo.finish(TEST_DAY, claimed[2].id, ok=False, error="boom")

    counters = await repo.counters(TEST_DAY)
    assert (counters.claimed, counters.ok, counters.failed) == (4, 2, 1)
    assert counters.in_progress == 1  # четвёртая ещё в работе
