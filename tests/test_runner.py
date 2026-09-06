"""T-22/T-23: заход целиком — лока, счётчики, устойчивость к падению игры.

HTTP подменён, БД настоящая: advisory-лока, строка в `runs` и статусы в
`processed_games` — ровно то, ради чего задача и делалась. Игры берут id из
служебного диапазона, а строка `day_cursor` за сегодня на время теста
подменяется и потом возвращается на место: заход всегда идёт по календарному
«сегодня», а портить реальное состояние дня тест не должен.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import httpx
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import BrowsePage, CatalogItem, PlatformInfo, Product, ScoreStats
from app.clients.metacritic import MetacriticError
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.ingest.runner import IngestRunner, advisory_lock
from app.main import app
from app.web import routes_admin

BASE_ID = 9_000_400_000
TODAY = datetime.now(UTC).date()
BROKEN_SLUG = "zzq-run-1"


def item(n: int) -> CatalogItem:
    return CatalogItem(id=BASE_ID + n, slug=f"zzq-run-{n}", title=f"ZZQ Run {n}")


class FakeMetacritic:
    """Три игры в New Releases, одна из них всегда падает на `get_product`."""

    def __init__(self, count: int = 3) -> None:
        self.items = [item(n) for n in range(count)]

    async def list_new_releases(self, limit: int = 20) -> list[CatalogItem]:
        return self.items[:limit]

    async def list_browse(self, offset: int, limit: int = 20) -> BrowsePage:
        return BrowsePage(items=[], offset=offset, total=0)

    async def get_product(self, slug: str) -> Product:
        if slug == BROKEN_SLUG:
            raise MetacriticError(500, f"/games/{slug}/web", "upstream is down")
        found = next(i for i in self.items if i.slug == slug)
        return Product(
            id=found.id,
            slug=slug,
            title=found.title,
            platforms=[PlatformInfo(slug="test-plat", name="TEST", is_lead=True, metascore=80)],
            genres=["Action"],
            raw={"slug": slug},
        )

    async def get_score_stats(self, slug: str, platform_slug: str, audience: str):
        return ScoreStats(score=7.5, count=10, sentiment="generally favorable")

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
async def clean_test_rows():
    try:
        saved = await snapshot_and_clear()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    try:
        yield
    finally:
        await restore(saved)
        await dispose_engine()


async def snapshot_and_clear() -> tuple[dict | None, int]:
    """Убирает курсор за сегодня (заход должен начаться с чистого дня) и запоминает его."""
    async with get_engine().begin() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM day_cursor WHERE day = :day"), {"day": TODAY}
            )
        ).one_or_none()
        last_run = await conn.scalar(text("SELECT coalesce(max(id), 0) FROM runs"))
        await conn.execute(text("DELETE FROM day_cursor WHERE day = :day"), {"day": TODAY})
        await conn.execute(
            text("DELETE FROM processed_games WHERE game_id >= :id"), {"id": BASE_ID}
        )
        await conn.execute(text("DELETE FROM games WHERE id >= :id"), {"id": BASE_ID})
    return (dict(row._mapping) if row else None, int(last_run))


async def restore(saved: tuple[dict | None, int]) -> None:
    row, last_run = saved
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM runs WHERE id > :id"), {"id": last_run})
        await conn.execute(
            text("DELETE FROM processed_games WHERE game_id >= :id"), {"id": BASE_ID}
        )
        await conn.execute(text("DELETE FROM games WHERE id >= :id"), {"id": BASE_ID})
        await conn.execute(text("DELETE FROM day_cursor WHERE day = :day"), {"day": TODAY})
        if row is not None:
            await conn.execute(
                text(
                    "INSERT INTO day_cursor (day, phase, browse_offset, runs_count,"
                    " claimed_count, created_at, updated_at) VALUES (:day, :phase,"
                    " :browse_offset, :runs_count, :claimed_count, :created_at, :updated_at)"
                ),
                row,
            )


async def fetch_run(run_id: int):
    async with get_engine().connect() as conn:
        return (await conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id})).one()


async def test_run_survives_a_failing_game():
    """Падение одной игры не роняет заход: остальные доезжают до БД."""
    result = await IngestRunner(FakeMetacritic()).run("manual")

    assert result.status == "ok"
    assert (result.games_claimed, result.games_ok, result.games_failed) == (3, 2, 1)

    run_row = await fetch_run(result.run_id)
    assert (run_row.status, run_row.trigger, run_row.day) == ("ok", "manual", TODAY)
    assert (run_row.games_ok, run_row.games_failed, run_row.pages_fetched) == (2, 1, 1)
    assert run_row.finished_at is not None

    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT game_id, status, error FROM processed_games "
                    "WHERE game_id >= :id ORDER BY game_id"
                ),
                {"id": BASE_ID},
            )
        ).all()
        stored = await conn.scalar(
            text("SELECT count(*) FROM games WHERE id >= :id"), {"id": BASE_ID}
        )

    statuses = {row.game_id: row.status for row in rows}
    assert statuses == {BASE_ID: "ok", BASE_ID + 1: "failed", BASE_ID + 2: "ok"}
    assert "MetacriticError" in next(r.error for r in rows if r.status == "failed")
    assert stored == 2


async def test_second_parallel_run_is_skipped():
    """Две одновременные корутины: вторая уходит по advisory-локе, БД игр не трогает."""
    runner = IngestRunner(FakeMetacritic())
    first, second = await asyncio.gather(runner.run("schedule"), runner.run("manual"))

    statuses = sorted([first.status, second.status])
    assert statuses == ["ok", "skipped_locked"]
    skipped = first if first.status == "skipped_locked" else second
    assert skipped.games_claimed == 0

    async with get_engine().connect() as conn:
        logged = await conn.scalar(
            text("SELECT count(*) FROM runs WHERE status = 'skipped_locked' AND day = :day"),
            {"day": TODAY},
        )
    assert logged == 1  # пропуск тоже виден в журнале заходов


async def test_empty_batch_is_logged_as_empty():
    result = await IngestRunner(FakeMetacritic(count=0)).run("schedule")
    assert result.status == "empty"
    assert (await fetch_run(result.run_id)).status == "empty"


async def test_is_locked_reports_running_ingest():
    runner = IngestRunner(FakeMetacritic())
    assert await runner.is_locked() is False
    async with advisory_lock() as acquired:
        assert acquired is True
        assert await runner.is_locked() is True


class StubRunner:
    def __init__(self, locked: bool) -> None:
        self.locked = locked
        self.started = asyncio.Event()

    async def is_locked(self) -> bool:
        return self.locked

    async def run(self, trigger: str):
        self.started.set()


async def admin_post(token: str | None) -> httpx.Response:
    headers = {"X-Admin-Token": token} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/admin/run", headers=headers)


async def test_admin_run_requires_token():
    assert (await admin_post(None)).status_code == 401


async def test_admin_run_starts_background_ingest(monkeypatch):
    stub = StubRunner(locked=False)
    monkeypatch.setattr(routes_admin, "get_runner", lambda: stub)

    response = await admin_post(get_settings().admin_token)

    assert response.status_code == 202
    await asyncio.wait_for(stub.started.wait(), timeout=1)


async def test_admin_run_conflicts_with_running_ingest(monkeypatch):
    stub = StubRunner(locked=True)
    monkeypatch.setattr(routes_admin, "get_runner", lambda: stub)

    response = await admin_post(get_settings().admin_token)

    assert response.status_code == 409
    assert not stub.started.is_set()
