"""Минимальный сквозной ingest: каталог → product → upsert (T-09).

Ни курсора, ни claim'а, ни блокировки параллельных запусков: это временный
путь, чтобы увидеть реальные данные и задеплоиться до появления часового
цикла. Полная версия — T-22 (runner) и T-23 (планировщик).
"""

from __future__ import annotations

import logging

from app.clients.dto import CatalogItem, ScoreStats
from app.clients.metacritic import MetacriticClient, MetacriticError
from app.ingest.upsert import upsert_game

log = logging.getLogger(__name__)


async def fetch_user_scores(client: MetacriticClient, product) -> dict[str, ScoreStats | None]:
    """Userscore по каждой платформе игры — отдельный вызов на платформу.

    Решение владельца: Userscore по всем платформам, отзывы — только по ведущей.
    """
    scores: dict[str, ScoreStats | None] = {}
    for platform in product.platforms:
        scores[platform.slug] = await client.get_score_stats(product.slug, platform.slug, "user")
    return scores


async def ingest_game(client: MetacriticClient, slug: str) -> int:
    """Обрабатывает одну игру целиком и возвращает её `id`."""
    product = await client.get_product(slug)
    user_scores = await fetch_user_scores(client, product)
    return await upsert_game(product, user_scores)


async def run_minimal_ingest(limit: int = 20) -> dict[str, int | list[str]]:
    """Проходит New Releases и складывает игры в БД.

    Падение одной игры не роняет заход: статусы обработки появятся в
    `processed_games` вместе с runner'ом (T-22), пока их некуда писать.
    """
    async with MetacriticClient() as client:
        items: list[CatalogItem] = await client.list_new_releases(limit)
        processed, failed = 0, []
        for item in items:
            try:
                await ingest_game(client, item.slug)
                processed += 1
            except MetacriticError as exc:  # TODO T-22: писать в processed_games.status='failed'
                log.warning("игра %s пропущена: %s", item.slug, exc)
                failed.append(item.slug)
    return {"processed": processed, "failed": len(failed), "failed_slugs": failed}
