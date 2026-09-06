"""T-18: выбор батча — фазы дня, добор страниц, исчерпание каталога.

Клиент Metacritic подменён: проверяется логика фаз и offset'ов, а не HTTP.
Репозитории — настоящие, на реальном Postgres: claim и есть то, что решает,
сколько игр вернётся, мок бы это спрятал.
"""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import BrowsePage, CatalogItem
from app.db import dispose_engine, get_engine
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


def selector(client: FakeCatalog) -> BatchSelector:
    return BatchSelector(client, DayCursorRepo(), ProcessedRepo())


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
