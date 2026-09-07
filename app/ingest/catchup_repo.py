"""Догоняющее обновление: игры, которым обход не довёз обложку или оценки.

Дневной курсор идёт по `sortBy=-releaseDate`, то есть систематически собирает
свежий край каталога — тот, который Metacritic ещё не наполнил. Игра, попавшая
в базу без обложки и без Metascore, дозаполнится только если снова попадёт под
дневной курсор; ушедшая из зоны обхода не дозаполнится никогда. Отсюда добор:
каждый заход берёт небольшую квоту таких игр сверх своей фазы.

Кандидаты идут по возрастанию `updated_at`, а `upsert_game` ставит
`updated_at = now()` на каждом проходе — очередь вращается сама. Игра, у
которой обложки нет и не будет (у источника пустой `images: []`), не занимает
квоту вечно: после прохода она уходит в хвост до следующего круга.

Заклеймленные сегодня игры отсекаются здесь же, а не только в `claim`: иначе
квота захода уходила бы на кандидатов, которых `claim` всё равно отбросит.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.clients.dto import CatalogItem
from app.db import get_engine

# Условие повторяет частичный индекс games_incomplete_idx из schema.sql:
# сканируется только неполная часть таблицы, а не весь каталог.
_PICK = text(
    """
    SELECT g.id, g.slug, g.title, g.release_date
    FROM games g
    WHERE (g.cover_path IS NULL OR g.best_metascore IS NULL)
      AND NOT EXISTS (
          SELECT 1 FROM processed_games p
          WHERE p.day = :day AND p.game_id = g.id
      )
    ORDER BY g.updated_at, g.id
    LIMIT :limit
    """
)


class CatchupRepo:
    """Кандидаты на догоняющее обновление из своей же базы."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine or get_engine()

    async def pick(self, day: date, limit: int) -> list[CatalogItem]:
        """До `limit` самых давно обновлявшихся неполных игр, не занятых сегодня.

        Возвращает `CatalogItem` — тот же тип, что и листинги Metacritic:
        дальше по конвейеру догоняющая игра ничем не отличается от каталожной.
        """
        if limit <= 0:
            return []
        async with self.engine.connect() as conn:
            rows = (await conn.execute(_PICK, {"day": day, "limit": limit})).mappings().all()
        return [
            CatalogItem(
                id=row["id"],
                slug=row["slug"],
                title=row["title"],
                release_date=row["release_date"],
            )
            for row in rows
        ]
