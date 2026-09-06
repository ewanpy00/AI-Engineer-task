"""T-22/T-23/T-30/T-35: заход целиком — лока, счётчики, устойчивость к падению
игры, подключённое к обходу резюме отзывов и события мониторинга.

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

from app.clients.dto import (
    BrowsePage,
    CatalogItem,
    PlatformInfo,
    Product,
    Quote,
    ReviewQuotes,
    ScoreStats,
)
from app.clients.metacritic import MetacriticError
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.events import Event, EventBus
from app.ingest.runner import IngestRunner, advisory_lock
from app.state import WorkerState
from app.llm.schemas import LlmResult, ReviewSummaryOut
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

    async def get_review_summary(self, slug: str, platform_slug: str, audience: str):
        return ReviewQuotes(
            positive=[Quote(text=f"great {slug}", bucket="positive")],
            negative=[Quote(text=f"buggy {slug}", bucket="negative")],
        )

    async def list_reviews(self, slug, platform_slug, audience, offset=0, limit=50):
        return []

    async def aclose(self) -> None:
        return None


SUMMARY = ReviewSummaryOut(liked=["раз", "два"], disliked=["три"], tldr="Итог.")


class FakeLlm:
    """Адаптер LLM без сети: считает вызовы, при `ok=False` всегда отказывает."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []
        self.resets = 0

    def reset_breaker(self) -> None:
        self.resets += 1

    async def summarize_reviews(self, *, audience, game_title, quotes, context=None):
        self.calls.append(f"{context['slug']}/{audience}")
        if not self.ok:
            return LlmResult(ok=False, error="403 PERMISSION_DENIED",
                             prompt_version=f"review_summary_{audience}.v1", model="fake")
        return LlmResult(ok=True, value=SUMMARY,
                         prompt_version=f"review_summary_{audience}.v1", model="fake")


def runner(
    client: FakeMetacritic | None = None,
    *,
    llm: FakeLlm | None = None,
    bus: EventBus | None = None,
    state: WorkerState | None = None,
) -> IngestRunner:
    """Заход с подменёнными Metacritic и LLM: в тестах сети нет ни там, ни там."""
    return IngestRunner(
        client or FakeMetacritic(),
        llm=llm or FakeLlm(),
        bus=bus or EventBus(),
        state=state or WorkerState(),
    )


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
    result = await runner().run("manual")

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


async def summaries() -> dict[tuple[int, str], dict]:
    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT * FROM review_summaries WHERE game_id >= :id"), {"id": BASE_ID}
            )
        ).mappings().all()
    return {(row["game_id"], row["audience"]): dict(row) for row in rows}


async def test_run_fills_review_summaries_without_a_separate_call():
    """Резюме собирается внутри обхода: отдельно пайплайн никто не дёргает."""
    llm = FakeLlm()

    result = await runner(llm=llm).run("manual")

    rows = await summaries()
    # две успешные игры × две аудитории; упавшая на get_product резюме не получает
    assert len(rows) == 4
    assert {row["status"] for row in rows.values()} == {"ok"}
    assert rows[(BASE_ID, "critic")]["liked"] == SUMMARY.liked
    assert rows[(BASE_ID, "user")]["tldr"] == SUMMARY.tldr
    assert sorted(llm.calls) == [
        "zzq-run-0/critic", "zzq-run-0/user", "zzq-run-2/critic", "zzq-run-2/user",
    ]
    assert (result.llm_calls, result.llm_failures) == (4, 0)

    run_row = await fetch_run(result.run_id)
    assert (run_row.llm_calls, run_row.llm_failures) == (4, 0)


async def test_broken_llm_does_not_change_the_game_status():
    """design §5.5: отказ LLM никогда не роняет обход каталога."""
    llm = FakeLlm(ok=False)

    result = await runner(llm=llm).run("manual")

    assert result.status == "ok"
    assert (result.games_ok, result.games_failed) == (2, 1)  # ровно как без LLM
    assert (result.llm_calls, result.llm_failures) == (4, 4)

    async with get_engine().connect() as conn:
        statuses = dict(
            (row.game_id, row.status)
            for row in (
                await conn.execute(
                    text("SELECT game_id, status FROM processed_games WHERE game_id >= :id"),
                    {"id": BASE_ID},
                )
            ).all()
        )
    assert statuses[BASE_ID] == "ok" and statuses[BASE_ID + 2] == "ok"
    assert {row["status"] for row in (await summaries()).values()} == {"llm_failed"}


async def test_pipeline_crash_leaves_the_game_ok():
    """Необработанное исключение внутри обогащения — не приговор игре."""

    class Exploding(FakeLlm):
        async def summarize_reviews(self, **kwargs):
            raise RuntimeError("адаптер сломан по-настоящему")

    result = await runner(llm=Exploding()).run("manual")

    assert result.status == "ok" and result.games_ok == 2
    # пайплайн ловит сбой сам и помечает аудиторию как невыполненную работу
    assert {row["status"] for row in (await summaries()).values()} == {"llm_failed"}


async def test_breaker_is_reset_at_the_start_of_every_run():
    """Circuit breaker живёт один заход: провайдер мог подняться за час."""
    llm = FakeLlm()
    ingest = runner(llm=llm)

    await ingest.run("manual")
    await ingest.run("schedule")

    assert llm.resets == 2


async def test_llm_is_skipped_when_disabled(monkeypatch):
    """LLM_ENABLED=false — прогон каталога без резюме и без строк llm_failed."""
    monkeypatch.setattr(get_settings(), "llm_enabled", False, raising=False)
    llm = FakeLlm()

    result = await runner(llm=llm).run("manual")

    assert result.games_ok == 2 and result.llm_calls == 0
    assert llm.calls == [] and await summaries() == {}


async def test_second_parallel_run_is_skipped():
    """Две одновременные корутины: вторая уходит по advisory-локе, БД игр не трогает."""
    ingest = runner()
    first, second = await asyncio.gather(ingest.run("schedule"), ingest.run("manual"))

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
    result = await runner(FakeMetacritic(count=0)).run("schedule")
    assert result.status == "empty"
    assert (await fetch_run(result.run_id)).status == "empty"


async def test_is_locked_reports_running_ingest():
    ingest = runner()
    assert await ingest.is_locked() is False
    async with advisory_lock() as acquired:
        assert acquired is True
        assert await ingest.is_locked() is True


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


async def test_run_publishes_events_for_every_step():
    """T-35: заход рассказывает о себе — из этого потока живёт страница статуса."""
    bus, state = EventBus(), WorkerState()
    with bus.subscribe() as subscription:
        result = await runner(bus=bus, state=state).run("manual")

        seen = []
        while (event := await subscription.next(0.05)) is not None:
            seen.append(event)

    kinds = [event.kind for event in seen]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert kinds.count("game_started") == 3
    assert kinds.count("game_done") == 2 and kinds.count("game_failed") == 1
    assert kinds.count("llm_call") == 2  # у упавшей игры до модели дело не дошло

    started = next(e for e in seen if e.kind == "run_started")
    assert started.payload["run_id"] == result.run_id
    assert started.payload["trigger"] == "manual"

    failed = next(e for e in seen if e.kind == "game_failed")
    assert failed.payload["slug"] == BROKEN_SLUG and "MetacriticError" in failed.payload["error"]

    # то же самое видно и в состоянии: события двигают его, а не отдельный код
    assert (state.status, state.run_id, state.current_game) == ("idle", None, None)
    assert (state.claimed, state.ok, state.failed) == (3, 2, 1)
    assert (state.llm_calls, state.llm_failures) == (4, 0)
    assert state.day == TODAY

    # подключившийся позже клиент берёт пропущенное из буфера шины
    assert [e.kind for e in bus.recent(200)] == kinds


async def test_state_shows_the_game_being_processed():
    """`current_game` меняется по ходу захода, а не только в его конце."""
    state = WorkerState()
    seen: list[tuple[str, int]] = []

    class WatchingLlm(FakeLlm):
        async def summarize_reviews(self, **kwargs):
            seen.append((state.current_game, state.ok))
            return await super().summarize_reviews(**kwargs)

    await runner(llm=WatchingLlm(), state=state, bus=EventBus()).run("manual")

    # в момент вызова модели по игре она уже `ok`, но из «в работе» ещё не ушла
    assert {title for title, _ in seen} <= {"ZZQ Run 0", "ZZQ Run 2"}
    assert seen and all(title is not None for title, _ in seen)


async def test_skipped_run_is_visible_but_does_not_reset_the_state():
    """Кнопка, отбитая локой, оставляет след в ленте и не трогает идущий заход."""
    bus, state = EventBus(), WorkerState()
    state.apply(Event(kind="run_started", payload={"run_id": 1, "trigger": "schedule"}))

    with bus.subscribe() as subscription:
        async with advisory_lock() as acquired:
            assert acquired
            result = await runner(bus=bus, state=state).run("manual")

        event = await subscription.next(0.05)

    assert result.status == "skipped_locked"
    assert event.kind == "run_finished" and event.payload["status"] == "skipped_locked"
    assert (state.status, state.run_id) == ("running", 1)
