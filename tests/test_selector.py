"""T-18: выбор батча — фазы дня, добор страниц, исчерпание каталога плюс
догоняющее обновление неполных игр из своей базы.

Клиент Metacritic подменён: проверяется логика фаз и offset'ов, а не HTTP.
Репозитории — настоящие, на реальном Postgres: claim и есть то, что решает,
сколько игр вернётся, мок бы это спрятал.

Тесты фаз идут с выключенным добором (`catchup_limit=0`): иначе в батч
попадали бы реальные неполные игры из базы разработчика, и проверка фазы
зависела бы от её содержимого. Добору отведён свой блок ниже, он заводит
кандидатов сам.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import BrowsePage, CatalogItem
from app.db import dispose_engine, get_engine
from app.ingest.catchup_repo import CatchupRepo
from app.ingest.day_cursor_repo import DayCursorRepo
from app.ingest.processed_repo import ProcessedRepo
from app.ingest.selector import BatchSelector

TEST_DAY = date(2999, 2, 2)
BASE_ID = 9_000_300_000


def item(n: int) -> CatalogItem:
    return CatalogItem(id=BASE_ID + n, slug=f"zzq-sel-{n}", title=f"ZZQ Sel {n}")


class FakeCatalog:
    """Каталог из `total` игр, порезанный на страницы по 20 — как SEE ALL."""

    def __init__(self, total: int = 200, new_releases: list[CatalogItem] | None = None) -> None:
        self.total = total
        self._new = new_releases if new_releases is not None else [item(n) for n in range(20)]
        self.calls: list[tuple[str, int]] = []

    async def list_new_releases(self, limit: int = 20) -> list[CatalogItem]:
        self.calls.append(("new_releases", limit))
        return self._new[:limit]

    async def list_browse(self, offset: int, limit: int = 20) -> BrowsePage:
        self.calls.append(("browse", offset))
        page = [item(n) for n in range(offset, min(offset + limit, self.total))]
        return BrowsePage(items=page, offset=offset, total=self.total)


def selector(client: FakeCatalog, catchup_limit: int = 0) -> BatchSelector:
    return BatchSelector(
        client, DayCursorRepo(), ProcessedRepo(), catchup_limit=catchup_limit
    )


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
        await conn.execute(text("DELETE FROM processed_games WHERE day = :day"), {"day": TEST_DAY})
        await conn.execute(text("DELETE FROM games WHERE slug LIKE 'zzq-cat-%'"))


async def test_first_run_of_day_goes_to_new_releases():
    client = FakeCatalog()
    batch = await selector(client).next_batch(TEST_DAY, run_id=1)

    assert [kind for kind, _ in client.calls] == ["new_releases"]
    assert batch.source == "new_releases"
    assert len(batch.items) == 20
    # день продолжится по SEE ALL с нулевого offset'а
    assert (batch.cursor_after.phase, batch.cursor_after.browse_offset) == ("browse", 0)


async def test_second_run_browses_and_skips_already_processed():
    """New Releases ⊂ SEE ALL: первая страница browse целиком дублирует первый заход."""
    client = FakeCatalog()
    sel = selector(client)
    await sel.next_batch(TEST_DAY, run_id=1)          # заклеймил игры 0..19
    batch = await sel.next_batch(TEST_DAY, run_id=2)

    assert client.calls[1:] == [("browse", 0), ("browse", 20)]  # первая страница пустая после дедупа
    assert batch.source == "browse"
    assert [i.id for i in batch.items] == [BASE_ID + n for n in range(20, 40)]
    assert batch.pages_fetched == 2
    assert batch.cursor_after.browse_offset == 40


async def test_top_up_stops_at_max_pages():
    """Пять страниц за заход — потолок, даже если игр так и не набралось."""
    client = FakeCatalog()
    sel = selector(client)
    # весь каталог до 200-й позиции уже обработан сегодня
    await ProcessedRepo().claim(TEST_DAY, 0, "browse", [item(n) for n in range(200)])
    await DayCursorRepo().get_or_create(TEST_DAY)
    await DayCursorRepo().advance(TEST_DAY, phase="browse", browse_offset=0, claimed=0)

    batch = await sel.next_batch(TEST_DAY, run_id=2)

    assert batch.items == []
    assert batch.pages_fetched == 5
    assert batch.cursor_after.browse_offset == 100  # курсор всё равно продвинут


async def test_exhausted_day_makes_no_http_calls():
    client = FakeCatalog(total=40)
    sel = selector(client)
    await sel.next_batch(TEST_DAY, run_id=1)   # new_releases: игры 0..19
    second = await sel.next_batch(TEST_DAY, run_id=2)
    assert second.cursor_after.phase == "exhausted"  # offset 40 упёрся в total

    calls_before = len(client.calls)
    third = await sel.next_batch(TEST_DAY, run_id=3)

    assert third.items == []
    assert third.pages_fetched == 0
    assert len(client.calls) == calls_before  # ни одного запроса к API


async def test_empty_page_ends_the_day():
    """Каталог кончился раньше, чем обещал `total` — заход не должен листать дальше."""
    client = FakeCatalog(total=1000)
    client.list_browse = _empty_browse(client)  # type: ignore[method-assign]
    await DayCursorRepo().get_or_create(TEST_DAY)
    await DayCursorRepo().advance(TEST_DAY, phase="browse", browse_offset=0, claimed=0)

    batch = await selector(client).next_batch(TEST_DAY, run_id=1)

    assert batch.pages_fetched == 1
    assert batch.cursor_after.phase == "exhausted"


def _empty_browse(client: FakeCatalog):
    async def list_browse(offset: int, limit: int = 20) -> BrowsePage:
        client.calls.append(("browse", offset))
        return BrowsePage(items=[], offset=offset, total=client.total)

    return list_browse


# --- Догоняющее обновление -------------------------------------------------
#
# Кандидат — игра из своей базы без обложки или без Metascore. Такие игры
# дневной курсор больше не принесёт: он идёт по `-releaseDate` и уходит от них
# вперёд. Проверяется ровно это: что добор их находит, что он занимает только
# свободное место в батче и что догоняющая игра клеймится как все остальные.

CATCH_BASE = BASE_ID + 900
# Кандидаты добора сортируются по updated_at, а в базе разработчика лежат
# настоящие неполные игры. Чтобы порядок задавал тест, а не содержимое базы,
# тестовые игры уводятся в заведомо давнее прошлое.
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)

_ADD_GAME = text(
    """
    INSERT INTO games (id, slug, title, cover_path, best_metascore, raw, updated_at)
    VALUES (:id, :slug, :title, :cover, :metascore, '{}'::jsonb, :updated_at)
    ON CONFLICT (id) DO UPDATE SET
        cover_path = EXCLUDED.cover_path,
        best_metascore = EXCLUDED.best_metascore,
        updated_at = EXCLUDED.updated_at
    """
)


async def add_game(
    n: int, *, cover: str | None = None, metascore: int | None = None, order: int = 0
) -> int:
    """Игра в базе. По умолчанию неполная (нет ни обложки, ни Metascore).

    `order` задаёт место в очереди добора: меньше — раньше обновлялась,
    значит раньше и придёт.
    """
    game_id = CATCH_BASE + n
    async with get_engine().begin() as conn:
        await conn.execute(
            _ADD_GAME,
            {
                "id": game_id,
                "slug": f"zzq-cat-{n}",
                "title": f"ZZQ Cat {n}",
                "cover": cover,
                "metascore": metascore,
                "updated_at": ANCIENT + timedelta(hours=order),
            },
        )
    return game_id


async def claimed_sources() -> dict[int, str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT game_id, source FROM processed_games WHERE day = :day"),
            {"day": TEST_DAY},
        )
    return {row.game_id: row.source for row in rows}


async def test_catchup_fills_the_room_left_by_the_phase():
    """New Releases принёс три игры — остаток добирается из базы, но не больше N."""
    for n in range(6):
        await add_game(n, order=n)

    client = FakeCatalog(new_releases=[item(0), item(1), item(2)])
    batch = await selector(client, catchup_limit=5).next_batch(TEST_DAY, run_id=1)

    assert batch.catchup_count == 5
    assert len(batch.items) == 8  # три каталожных плюс пять догоняющих
    # очередь идёт по updated_at ASC: шестая игра ждёт следующего захода
    assert [i.id for i in batch.items[3:]] == [CATCH_BASE + n for n in range(5)]


async def test_catchup_takes_only_incomplete_games():
    complete = await add_game(0, cover="a/b.jpg", metascore=80, order=0)
    no_cover = await add_game(1, metascore=80, order=1)
    no_score = await add_game(2, cover="a/c.jpg", order=2)

    batch = await selector(FakeCatalog(new_releases=[]), catchup_limit=2).next_batch(
        TEST_DAY, run_id=1
    )

    # игра с обложкой и оценкой не попадает в очередь, хотя обновлялась раньше всех
    assert [i.id for i in batch.items] == [no_cover, no_score]
    assert complete not in {i.id for i in batch.items}


async def test_catchup_does_not_grow_the_batch():
    """Фаза набрала свои двадцать — добора нет, батч остаётся прежнего размера."""
    for n in range(5):
        await add_game(n, order=n)

    client = FakeCatalog()  # двадцать игр в New Releases
    batch = await selector(client, catchup_limit=5).next_batch(TEST_DAY, run_id=1)

    assert batch.catchup_count == 0
    assert len(batch.items) == 20


async def test_catchup_game_is_claimed_and_not_taken_twice_a_day():
    """Догоняющая игра идёт через тот же claim: сегодня она больше не кандидат."""
    first_id = await add_game(0, order=0)
    second_id = await add_game(1, order=1)

    client = FakeCatalog(new_releases=[item(0)])
    batch = await selector(client, catchup_limit=2).next_batch(TEST_DAY, run_id=1)

    assert {i.id for i in batch.items} == {BASE_ID, first_id, second_id}
    # в журнале дня у догоняющих игр свой источник — их видно отдельно от каталожных
    sources = await claimed_sources()
    assert sources[first_id] == sources[second_id] == "catchup"
    assert sources[BASE_ID] == "new_releases"
    # и очередь их больше не отдаёт: ни следующему заходу, ни кнопке
    again = await CatchupRepo().pick(TEST_DAY, 50)
    assert not ({i.id for i in again} & {first_id, second_id})


async def test_catchup_tops_up_a_browse_run_that_found_nothing_new():
    """Заход по SEE ALL, вычерпанному дедупом, всё равно делает полезную работу."""
    await ProcessedRepo().claim(TEST_DAY, 0, "browse", [item(n) for n in range(200)])
    await DayCursorRepo().get_or_create(TEST_DAY)
    await DayCursorRepo().advance(TEST_DAY, phase="browse", browse_offset=0, claimed=0)
    for n in range(3):
        await add_game(n, order=n)

    batch = await selector(FakeCatalog(), catchup_limit=3).next_batch(TEST_DAY, run_id=2)

    assert batch.pages_fetched == 5  # фаза отработала как раньше
    assert batch.catchup_count == 3
    assert {i.id for i in batch.items} == {CATCH_BASE + n for n in range(3)}


async def test_catchup_is_off_when_the_limit_is_zero():
    await add_game(0, order=0)

    batch = await selector(FakeCatalog(new_releases=[]), catchup_limit=0).next_batch(
        TEST_DAY, run_id=1
    )

    assert batch.items == []
    assert batch.catchup_count == 0
