"""Курсор обхода за сутки (T-18, design §3.3, ADR-6).

Отдельной джобы-сброса в полночь нет и не будет: курсор ключуется UTC-датой,
и первый заход новых суток просто не находит строку за сегодня — создаёт её
со значениями по умолчанию. Это и есть сброс. Он идемпотентен (два
параллельных захода не создадут два курсора), переживает простой сервиса
в 00:00 и оставляет историю прошлых дней в таблице.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine

Phase = Literal["new_releases", "browse", "exhausted"]

PHASES: tuple[Phase, ...] = ("new_releases", "browse", "exhausted")


@dataclass(frozen=True)
class DayCursor:
    """Состояние обхода на конкретные UTC-сутки."""

    day: date
    phase: Phase
    browse_offset: int
    runs_count: int
    claimed_count: int


_COLUMNS = "day, phase, browse_offset, runs_count, claimed_count"

# RETURNING при конфликте не вернёт ничего (строку вставили не мы), поэтому
# за INSERT'ом всегда идёт SELECT — оба в одной транзакции.
_INSERT = text(
    f"INSERT INTO day_cursor (day) VALUES (:day) ON CONFLICT (day) DO NOTHING RETURNING {_COLUMNS}"
)
_SELECT = text(f"SELECT {_COLUMNS} FROM day_cursor WHERE day = :day")

_ADVANCE = text(
    f"""
    UPDATE day_cursor SET
        phase         = :phase,
        browse_offset = :browse_offset,
        claimed_count = claimed_count + :claimed,
        runs_count    = runs_count + 1,
        updated_at    = now()
    WHERE day = :day
    RETURNING {_COLUMNS}
    """
)


def _cursor(row: Row) -> DayCursor:
    return DayCursor(
        day=row.day,
        phase=row.phase,
        browse_offset=row.browse_offset,
        runs_count=row.runs_count,
        claimed_count=row.claimed_count,
    )


class DayCursorRepo:
    """Реализация `DayCursorRepo` из design §4.2."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine or get_engine()

    async def get_or_create(self, day: date) -> DayCursor:
        """Курсор за `day`; первый вызов в сутки создаёт его (`phase='new_releases'`)."""
        async with self.engine.begin() as conn:
            row = (await conn.execute(_INSERT, {"day": day})).one_or_none()
            if row is None:  # строку успел вставить кто-то другой — читаем её
                row = (await conn.execute(_SELECT, {"day": day})).one()
        return _cursor(row)

    async def advance(
        self, day: date, *, phase: str, browse_offset: int, claimed: int
    ) -> DayCursor:
        """Фиксирует итог одного захода: фаза, offset, +claimed игр, +1 заход."""
        if phase not in PHASES:
            raise ValueError(f"неизвестная фаза {phase!r}")
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    _ADVANCE,
                    {
                        "day": day,
                        "phase": phase,
                        "browse_offset": browse_offset,
                        "claimed": claimed,
                    },
                )
            ).one_or_none()
        if row is None:
            raise LookupError(f"курсор за {day} не найден: advance до get_or_create")
        return _cursor(row)
