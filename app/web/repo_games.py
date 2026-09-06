"""Чтение игр для веб-слоя. Core/raw SQL, без ORM-моделей (ADR-3)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.db import get_engine

# Платформы подтягиваются агрегатом, а не вторым запросом на карточку:
# список из 20 игр не должен превращаться в 21 обращение к БД.
_LIST_GAMES = text(
    """
    SELECT g.id, g.slug, g.title, g.cover_path, g.release_date,
           g.best_metascore, g.best_userscore, g.genres_cache,
           COALESCE(
               array_agg(p.platform_name ORDER BY p.is_lead DESC, p.platform_name)
               FILTER (WHERE p.platform_slug IS NOT NULL),
               '{}'
           ) AS platforms
    FROM games g
    LEFT JOIN game_platforms p ON p.game_id = g.id
    GROUP BY g.id
    ORDER BY g.first_seen_at DESC, g.id DESC
    LIMIT :limit
    """
)


async def list_games(limit: int = 60) -> list[dict[str, Any]]:
    """Последние добавленные игры. Поиск, фильтр и сортировка — T-15."""
    async with get_engine().connect() as conn:
        rows = (await conn.execute(_LIST_GAMES, {"limit": limit})).mappings().all()
    return [dict(row) for row in rows]
