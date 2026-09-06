"""Журнал заходов для страницы статуса (T-38).

Единственное, что странице нужно из БД: `WorkerState` рассказывает про
сегодня и про сейчас, а история заходов — только здесь.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.db import get_engine

RUNS_LIMIT = 10

_RECENT_RUNS = text(
    """
    SELECT id, day, trigger, status, phase, pages_fetched, games_claimed,
           games_ok, games_failed, llm_calls, llm_failures,
           started_at, finished_at, error
    FROM runs
    ORDER BY started_at DESC
    LIMIT :limit
    """
)


async def recent_runs(limit: int = RUNS_LIMIT) -> list[dict[str, Any]]:
    async with get_engine().connect() as conn:
        rows = (await conn.execute(_RECENT_RUNS, {"limit": limit})).mappings().all()
    return [dict(row) for row in rows]
