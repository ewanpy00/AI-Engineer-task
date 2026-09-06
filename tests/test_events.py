"""T-35: шина событий и состояние воркера.

Шина проверяется без БД и без веба — это чистая структура данных, и важны у
неё ровно два свойства: fan-out на всех подписчиков и то, что медленный
подписчик теряет старые события, а не тормозит воркера. Восстановление
счётчиков идёт в настоящий Postgres: смысл задачи в том, что после рестарта
цифры приходят из `processed_games`/`runs`, и мок это не покажет.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.db import dispose_engine, get_engine
from app.events import Event, EventBus
from app.state import WorkerState, reset_state, restore

BASE_ID = 9_000_600_000
DAY = date(2026, 4, 1)  # заведомо чужой день: реальные строки за сегодня не трогаем


def ev(kind: str, **payload: object) -> Event:
    return Event(kind=kind, payload=payload)


async def test_publish_reaches_every_subscriber():
    bus = EventBus()
    with bus.subscribe() as first, bus.subscribe() as second:
        bus.publish(ev("run_started", run_id=1))

        assert (await first.next(1)).payload == {"run_id": 1}
        assert (await second.next(1)).payload == {"run_id": 1}


async def test_publish_never_waits_for_a_slow_subscriber():
    """Подписчик, который не читает очередь, не должен тормозить обход."""
    bus = EventBus(queue_size=2)
    with bus.subscribe() as slow:
        for n in range(200):
            bus.publish(ev("game_done", n=n))

        # drop-oldest: в очереди последние два события, а не первые
        assert [(await slow.next(1)).payload["n"] for _ in range(2)] == [198, 199]
        assert await slow.next(0.05) is None


async def test_slow_subscriber_does_not_starve_the_others():
    bus = EventBus(queue_size=1)
    with bus.subscribe() as slow, bus.subscribe() as fast:
        for n in range(5):
            bus.publish(ev("game_done", n=n))
            # тот, кто читает, не теряет ничего, хотя соседняя очередь переполнена
            assert (await fast.next(1)).payload["n"] == n

        assert (await slow.next(1)).payload["n"] == 4  # отставшему досталось последнее


async def test_recent_keeps_publication_order_and_ring_size():
    bus = EventBus(buffer_size=3)
    for n in range(5):
        bus.publish(ev("game_done", n=n))

    assert [e.payload["n"] for e in bus.recent(50)] == [2, 3, 4]
    assert [e.payload["n"] for e in bus.recent(2)] == [3, 4]


async def test_unsubscribed_queue_stops_receiving():
    bus = EventBus()
    subscription = bus.subscribe()
    subscription.close()

    bus.publish(ev("run_started"))
    assert bus.subscribers == 0
    assert await subscription.next(0.05) is None


async def test_timeout_does_not_break_the_subscription():
    """Keep-alive в SSE — это таймаут ожидания; после него подписка живая."""
    bus = EventBus()
    with bus.subscribe() as subscription:
        assert await subscription.next(0.01) is None

        bus.publish(ev("game_started", slug="a"))
        assert (await subscription.next(1)).payload["slug"] == "a"


def test_state_follows_the_event_stream():
    state = WorkerState(day=DAY)

    state.apply(ev("run_started", run_id=7, trigger="manual", day=DAY))
    state.apply(ev("counters", phase="browse", browse_offset=40, claimed=3, pages=2))
    state.apply(ev("game_started", slug="a", title="Alpha"))
    state.apply(ev("game_started", slug="b", title="Beta"))

    assert (state.status, state.run_id, state.trigger) == ("running", 7, "manual")
    assert (state.phase, state.browse_offset, state.claimed) == ("browse", 40, 3)
    # обход идёт с параллелизмом 4: «текущая» игра — первая из тех, что в работе
    assert state.current_game == "Alpha"
    assert state.in_progress == 3

    state.apply(ev("game_done", slug="a"))
    state.apply(ev("game_failed", slug="b", error="boom"))
    state.apply(ev("llm_call", slug="a", calls=2, failures=1))

    assert (state.ok, state.failed, state.in_progress) == (1, 1, 1)
    assert (state.llm_calls, state.llm_failures) == (2, 1)
    assert state.current_game is None

    state.apply(ev("run_finished", run_id=7, status="ok"))
    assert (state.status, state.run_id) == ("idle", None)
    assert (state.ok, state.failed) == (1, 1)  # счётчики дневные, заход их не обнуляет


def test_skipped_run_does_not_stop_the_running_one():
    """Ручной запуск, отбитый локой, публикует `run_finished` без `run_id`."""
    state = WorkerState(day=DAY)
    state.apply(ev("run_started", run_id=7, trigger="schedule", day=DAY))
    state.apply(ev("game_started", slug="a", title="Alpha"))

    state.apply(ev("run_finished", run_id=None, status="skipped_locked"))

    assert (state.status, state.run_id, state.current_game) == ("running", 7, "Alpha")


def test_new_day_resets_counters():
    state = WorkerState(day=DAY, claimed=20, ok=18, failed=2, browse_offset=60, phase="browse")

    state.apply(ev("run_started", run_id=1, trigger="schedule", day=DAY + timedelta(days=1)))

    assert (state.claimed, state.ok, state.failed) == (0, 0, 0)
    assert (state.phase, state.browse_offset) == ("new_releases", 0)


def test_last_event_at_tracks_the_stream():
    state = WorkerState(day=DAY)
    ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    state.apply(Event(kind="game_done", payload={"slug": "a"}, ts=ts))

    assert state.last_event_at == ts


@pytest.fixture
async def day_rows():
    """Заход за прошлый день в БД: ровно то, что должен найти рестарт."""
    try:
        async with get_engine().begin() as conn:
            await _clear(conn)
            run_id = await conn.scalar(
                text(
                    "INSERT INTO runs (day, trigger, status, phase, games_claimed, games_ok,"
                    " games_failed, llm_calls, llm_failures) VALUES (:day, 'schedule', 'ok',"
                    " 'browse', 3, 2, 1, 4, 1) RETURNING id"
                ),
                {"day": DAY},
            )
            await conn.execute(
                text(
                    "INSERT INTO day_cursor (day, phase, browse_offset) "
                    "VALUES (:day, 'browse', 60)"
                ),
                {"day": DAY},
            )
            await conn.execute(
                text(
                    "INSERT INTO processed_games (day, game_id, slug, source, status, run_id)"
                    " SELECT :day, CAST(:base AS bigint) + n, 'zzq-state-' || n, 'browse',"
                    " (ARRAY['ok','ok','failed'])[n + 1], :run"
                    " FROM generate_series(0, 2) AS n"
                ),
                {"day": DAY, "base": BASE_ID, "run": run_id},
            )
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    reset_state()
    try:
        yield
    finally:
        async with get_engine().begin() as conn:
            await _clear(conn)
        reset_state()
        await dispose_engine()


async def _clear(conn) -> None:
    await conn.execute(text("DELETE FROM runs WHERE day = :day"), {"day": DAY})
    await conn.execute(text("DELETE FROM day_cursor WHERE day = :day"), {"day": DAY})
    await conn.execute(text("DELETE FROM processed_games WHERE day = :day"), {"day": DAY})


async def test_restore_takes_counters_from_the_database(day_rows):
    """После рестарта счётчики за день не обнуляются — их источник в Postgres."""
    state = await restore(DAY)

    assert (state.claimed, state.ok, state.failed) == (3, 2, 1)
    assert (state.llm_calls, state.llm_failures) == (4, 1)
    assert (state.phase, state.browse_offset) == ("browse", 60)
    assert (state.status, state.run_id, state.current_game) == ("idle", None, None)


async def test_restore_survives_a_day_without_history():
    """Первый старт в новые сутки: истории нет, падать не на чем."""
    reset_state()
    try:
        state = await restore(date(2026, 4, 2))
        assert (state.claimed, state.ok, state.failed) == (0, 0, 0)
        assert (state.phase, state.status) == ("new_releases", "idle")
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    finally:
        reset_state()
        await dispose_engine()
