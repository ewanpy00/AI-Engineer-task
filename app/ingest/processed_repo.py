"""Журнал захвата игр на день (T-18, design §3.3, ADR-6).

`processed_games` — не отчёт постфактум, а claim: кандидаты вставляются сюда
*до* обработки, и обрабатывается только то, что вернул `INSERT ... ON CONFLICT
DO NOTHING RETURNING`. Одной операцией это закрывает пересечение
New Releases ⊂ SEE ALL, дубли внутри страницы, дубли между заходами при
нестабильной сортировке `-releaseDate` (research RISK #1) и гонку ручного
запуска с плановым.

Упавшая сегодня игра остаётся заклеймленной: иначе одна «ядовитая» игра
выедала бы квоту в 20 игр каждый час. Её починит следующий день или ручной
переклейм (T-49, OQ-7) — `reclaim_failed`, отдельный проход только по кнопке.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.clients.dto import CatalogItem
from app.db import get_engine

ERROR_LIMIT = 1000  # текст ошибки в БД: хватает на сообщение, не хватает на дамп
RECLAIM_LIMIT = 20  # OQ-7: сколько failed-игр забирает один ручной запуск


@dataclass(frozen=True)
class DayCounters:
    """Итоги дня из `processed_games` — источник счётчиков после рестарта (T-36)."""

    day: date
    claimed: int  # всего заклеймлено за день, включая ok и failed
    ok: int
    failed: int

    @property
    def in_progress(self) -> int:
        return self.claimed - self.ok - self.failed


_CLAIM = text(
    """
    INSERT INTO processed_games (day, game_id, slug, source, run_id)
    SELECT :day, x.id, x.slug, :source, :run_id
    FROM unnest(CAST(:ids AS bigint[]), CAST(:slugs AS text[])) AS x(id, slug)
    ON CONFLICT (day, game_id) DO NOTHING
    RETURNING game_id
    """
)

_FINISH = text(
    """
    UPDATE processed_games
    SET status = :status, error = :error, finished_at = now()
    WHERE day = :day AND game_id = :game_id
    """
)

# OQ-7: переклейм — это UPDATE уже существующей строки, а не второй claim:
# ключ (day, game_id) один на сутки, второй строки по игре за день быть не может.
# `day_cursor` здесь не участвует вовсе — проход идёт сверх дневной квоты и
# `browse_offset` не двигает.
_RECLAIM_FAILED = text(
    """
    WITH failed AS (
        SELECT game_id, slug
        FROM processed_games
        WHERE day = :day AND status = 'failed'
        ORDER BY claimed_at, game_id
        LIMIT :limit
    ), picked AS (
        -- название лежит в `games`, а игры там может не быть: она могла упасть
        -- как раз на первом обращении к Metacritic, до вставки в каталог
        SELECT f.game_id, f.slug, coalesce(g.title, f.slug) AS title, g.release_date
        FROM failed f LEFT JOIN games g ON g.id = f.game_id
    )
    UPDATE processed_games p
    SET status = 'claimed', source = 'manual', run_id = :run_id,
        error = NULL, finished_at = NULL, claimed_at = now()
    FROM picked
    WHERE p.day = :day AND p.game_id = picked.game_id
    RETURNING picked.game_id, picked.slug, picked.title, picked.release_date
    """
)

_COUNTERS = text(
    """
    SELECT count(*)                                   AS claimed,
           count(*) FILTER (WHERE status = 'ok')      AS ok,
           count(*) FILTER (WHERE status = 'failed')  AS failed
    FROM processed_games WHERE day = :day
    """
)


class ProcessedRepo:
    """Реализация `ProcessedRepo` из design §4.2."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine or get_engine()

    async def claim(
        self, day: date, run_id: int | None, source: str, items: Sequence[CatalogItem]
    ) -> list[CatalogItem]:
        """Захватывает кандидатов и возвращает только тех, кого сегодня ещё не брали."""
        # Дубли внутри одной страницы ON CONFLICT DO NOTHING переживёт, но
        # порядок и соответствие id → item удобнее держать в питоне.
        unique: dict[int, CatalogItem] = {}
        for item in items:
            unique.setdefault(item.id, item)
        if not unique:
            return []

        async with self.engine.begin() as conn:
            rows = await conn.execute(
                _CLAIM,
                {
                    "day": day,
                    "run_id": run_id,
                    "source": source,
                    "ids": list(unique),
                    "slugs": [item.slug for item in unique.values()],
                },
            )
            claimed = {row.game_id for row in rows}
        return [item for game_id, item in unique.items() if game_id in claimed]

    async def finish(
        self, day: date, game_id: int, *, ok: bool, error: str | None = None
    ) -> None:
        """Проставляет исход обработки. Переклейма в тот же день не даёт ни тот, ни другой."""
        async with self.engine.begin() as conn:
            await conn.execute(
                _FINISH,
                {
                    "day": day,
                    "game_id": game_id,
                    "status": "ok" if ok else "failed",
                    "error": error[:ERROR_LIMIT] if error else None,
                },
            )

    async def reclaim_failed(
        self, day: date, run_id: int | None, limit: int = RECLAIM_LIMIT
    ) -> list[CatalogItem]:
        """Возвращает в работу до `limit` упавших сегодня игр (OQ-7, T-49).

        Только для ручного запуска: плановый заход упавшую игру до смены суток
        не берёт (ADR-6). Проход отдельный и сверх обычной квоты в 20 игр,
        `day_cursor.browse_offset` не трогается.

        Игра снова получает `status='claimed'` и `source='manual'`, а прошлая
        ошибка стирается: строка описывает текущую попытку, а не историю
        (истории по игре в схеме и нет — ключ один на сутки). Заклеймленная
        строка остаётся на месте, поэтому переклеймленная игра по-прежнему не
        попадёт ни в плановый заход, ни во второй переклейм в этом же дне —
        ровно до того, как `finish` снова поставит ей `failed`.
        """
        if limit <= 0:
            return []
        async with self.engine.begin() as conn:
            rows = (
                await conn.execute(
                    _RECLAIM_FAILED, {"day": day, "run_id": run_id, "limit": limit}
                )
            ).mappings().all()
        return [
            CatalogItem(
                id=row["game_id"],
                slug=row["slug"],
                title=row["title"],
                release_date=row["release_date"],
            )
            for row in rows
        ]

    async def counters(self, day: date) -> DayCounters:
        async with self.engine.connect() as conn:
            row = (await conn.execute(_COUNTERS, {"day": day})).one()
        return DayCounters(day=day, claimed=row.claimed, ok=row.ok, failed=row.failed)
