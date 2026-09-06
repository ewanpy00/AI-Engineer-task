"""Запись игры в БД одной транзакцией (T-08).

`games` + `game_platforms` + `game_genres` пишутся вместе или не пишутся вовсе:
рассинхрон «игра есть, платформ нет» ломает и список, и похожие игры.
Внешних вызовов внутри транзакции нет — Userscore добирается заранее
(`app/ingest/runner.py`) и приходит сюда готовым словарём.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.dto import PlatformInfo, Product, ScoreStats
from app.db import get_engine

_UPSERT_GAME = text(
    """
    INSERT INTO games (
        id, slug, title, description, developer, publisher, esrb_rating,
        release_date, cover_path, video_url, lead_platform_slug,
        best_metascore, best_userscore, genres_cache, raw
    ) VALUES (
        :id, :slug, :title, :description, :developer, :publisher, :esrb_rating,
        :release_date, :cover_path, :video_url, :lead_platform_slug,
        :best_metascore, :best_userscore, CAST(:genres AS text[]), CAST(:raw AS jsonb)
    )
    ON CONFLICT (id) DO UPDATE SET
        slug               = EXCLUDED.slug,
        title              = EXCLUDED.title,
        description        = EXCLUDED.description,
        developer          = EXCLUDED.developer,
        publisher          = EXCLUDED.publisher,
        esrb_rating        = EXCLUDED.esrb_rating,
        release_date       = EXCLUDED.release_date,
        cover_path         = EXCLUDED.cover_path,
        video_url          = EXCLUDED.video_url,
        lead_platform_slug = EXCLUDED.lead_platform_slug,
        best_metascore     = EXCLUDED.best_metascore,
        best_userscore     = EXCLUDED.best_userscore,
        genres_cache       = EXCLUDED.genres_cache,
        raw                = EXCLUDED.raw,
        updated_at         = now()
    RETURNING id
    """
)

_UPSERT_PLATFORM = text(
    """
    INSERT INTO game_platforms (
        game_id, platform_slug, platform_name, is_lead, release_date,
        metascore, metascore_count, metascore_sentiment,
        userscore, userscore_count, userscore_sentiment
    ) VALUES (
        :game_id, :platform_slug, :platform_name, :is_lead, :release_date,
        :metascore, :metascore_count, :metascore_sentiment,
        :userscore, :userscore_count, :userscore_sentiment
    )
    ON CONFLICT (game_id, platform_slug) DO UPDATE SET
        platform_name       = EXCLUDED.platform_name,
        is_lead             = EXCLUDED.is_lead,
        release_date        = EXCLUDED.release_date,
        metascore           = EXCLUDED.metascore,
        metascore_count     = EXCLUDED.metascore_count,
        metascore_sentiment = EXCLUDED.metascore_sentiment,
        userscore           = EXCLUDED.userscore,
        userscore_count     = EXCLUDED.userscore_count,
        userscore_sentiment = EXCLUDED.userscore_sentiment,
        updated_at          = now()
    """
)

# Платформа могла исчезнуть из ответа API (переименование slug, отмена релиза) —
# иначе она осталась бы в карточке навсегда.
_DROP_STALE_PLATFORMS = text(
    "DELETE FROM game_platforms WHERE game_id = :game_id "
    "AND platform_slug <> ALL(CAST(:keep AS text[]))"
)
_UPSERT_GENRE = text(
    "INSERT INTO game_genres (game_id, genre) VALUES (:game_id, :genre) ON CONFLICT DO NOTHING"
)
_DROP_STALE_GENRES = text(
    "DELETE FROM game_genres WHERE game_id = :game_id AND genre <> ALL(CAST(:keep AS text[]))"
)


async def upsert_game(
    product: Product,
    user_scores: Mapping[str, ScoreStats | None] | None = None,
    *,
    conn: AsyncConnection | None = None,
) -> int:
    """Идемпотентно кладёт игру в БД и возвращает её `id`.

    `user_scores`: platform_slug → Userscore этой платформы (или `None`, если
    оценок нет). Metascore приходит внутри `product`, отдельный вызов не нужен.
    """
    if conn is not None:
        return await _upsert(conn, product, user_scores or {})
    async with get_engine().begin() as own_conn:
        return await _upsert(own_conn, product, user_scores or {})


def _platform_row(game_id: int, platform: PlatformInfo, user: ScoreStats | None) -> dict:
    return {
        "game_id": game_id,
        "platform_slug": platform.slug,
        "platform_name": platform.name,
        "is_lead": platform.is_lead,
        "release_date": platform.release_date,
        "metascore": platform.metascore,
        "metascore_count": platform.metascore_count,
        "metascore_sentiment": platform.metascore_sentiment,
        "userscore": user.score if user else None,
        "userscore_count": user.count if user else None,
        "userscore_sentiment": user.sentiment if user else None,
    }


async def _upsert(conn: AsyncConnection, product: Product, user_scores: Mapping[str, ScoreStats | None]) -> int:
    metascores = [p.metascore for p in product.platforms if p.metascore is not None]
    userscores = [s.score for s in user_scores.values() if s is not None and s.score is not None]
    lead = product.lead_platform

    game_id = await conn.scalar(
        _UPSERT_GAME,
        {
            "id": product.id,
            "slug": product.slug,
            "title": product.title,
            "description": product.description,
            "developer": product.developer,
            "publisher": product.publisher,
            "esrb_rating": product.esrb_rating,
            "release_date": product.release_date,
            "cover_path": product.cover_path,
            "video_url": product.video_url,
            "lead_platform_slug": lead.slug if lead else None,
            "best_metascore": max(metascores) if metascores else None,
            "best_userscore": max(userscores) if userscores else None,
            "genres": list(dict.fromkeys(product.genres)),
            "raw": json.dumps(product.raw, ensure_ascii=False),
        },
    )

    if product.platforms:
        await conn.execute(
            _UPSERT_PLATFORM,
            [_platform_row(game_id, p, user_scores.get(p.slug)) for p in product.platforms],
        )
    await conn.execute(
        _DROP_STALE_PLATFORMS, {"game_id": game_id, "keep": [p.slug for p in product.platforms]}
    )

    genres = list(dict.fromkeys(product.genres))
    if genres:
        await conn.execute(_UPSERT_GENRE, [{"game_id": game_id, "genre": g} for g in genres])
    await conn.execute(_DROP_STALE_GENRES, {"game_id": game_id, "keep": genres})
    return int(game_id)
