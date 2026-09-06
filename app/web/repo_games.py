"""Чтение игр для веб-слоя. Core/raw SQL, без ORM-моделей (ADR-3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.db import get_engine

PER_PAGE = 24

DEFAULT_SORT = "new"
# Ключ сортировки приходит из query-строки, поэтому в SQL попадает не он сам,
# а фрагмент из этой таблицы: подстановки пользовательского текста в ORDER BY нет.
_SORT_SQL = {
    "new": "g.first_seen_at DESC, g.id DESC",
    "metascore": "g.best_metascore DESC NULLS LAST, g.id DESC",
    "userscore": "g.best_userscore DESC NULLS LAST, g.id DESC",
}
SORTS = tuple(_SORT_SQL)

# Платформы собираются боковым подзапросом, а не JOIN + GROUP BY: так ORDER BY
# и LIMIT остаются на самой `games` и идут по индексам games_first_seen_idx /
# games_best_metascore_idx / games_best_userscore_idx, а не после агрегации.
# Один запрос на страницу целиком — 24 игры не должны давать 25 обращений к БД.
_LIST_TEMPLATE = """
    SELECT g.id, g.slug, g.title, g.cover_path, g.release_date,
           g.best_metascore, g.best_userscore, g.genres_cache,
           COALESCE(pl.platforms, ARRAY[]::text[]) AS platforms
    FROM games g
    LEFT JOIN LATERAL (
        SELECT array_agg(p.platform_name ORDER BY p.is_lead DESC, p.platform_name) AS platforms
        FROM game_platforms p
        WHERE p.game_id = g.id
    ) pl ON true
    {where}
    ORDER BY {order}
    LIMIT :limit OFFSET :offset
"""

_COUNT_TEMPLATE = "SELECT count(*) FROM games g {where}"

# Фильтр по платформе — EXISTS, а не JOIN: игра с двумя платформами не должна
# появиться в списке дважды. Идёт по game_platforms_slug_idx.
_PLATFORM_FILTER = (
    "EXISTS (SELECT 1 FROM game_platforms p "
    "WHERE p.game_id = g.id AND p.platform_slug = :platform)"
)

_LIST_PLATFORMS = text(
    """
    SELECT platform_slug, min(platform_name) AS platform_name, count(*) AS games_count
    FROM game_platforms
    GROUP BY platform_slug
    ORDER BY platform_name
    """
)


@dataclass(frozen=True)
class GamesPage:
    games: list[dict[str, Any]]
    total: int
    page: int
    pages: int


def _like_pattern(q: str) -> str:
    """`%q%` с экранированием: символы LIKE в запросе пользователя — это текст."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(q: str, platform: str) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if q:
        # ILIKE '%…%' обслуживается gin-индексом games_title_trgm_idx (pg_trgm).
        clauses.append("g.title ILIKE :title_pattern")
        params["title_pattern"] = _like_pattern(q)
    if platform:
        clauses.append(_PLATFORM_FILTER)
        params["platform"] = platform
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


async def list_games(
    q: str = "", platform: str = "", sort: str = DEFAULT_SORT, page: int = 1, per_page: int = PER_PAGE
) -> GamesPage:
    """Страница списка игр с поиском, фильтром и сортировкой.

    `sort` и `page` считаются уже нормализованными (`normalize_sort`/`normalize_page`).
    """
    where, params = _where(q, platform)
    list_sql = text(_LIST_TEMPLATE.format(where=where, order=_SORT_SQL[sort]))
    count_sql = text(_COUNT_TEMPLATE.format(where=where))

    async with get_engine().connect() as conn:
        total = int(await conn.scalar(count_sql, params) or 0)
        pages = max(1, -(-total // per_page))
        page = min(page, pages)
        rows = (
            await conn.execute(
                list_sql, params | {"limit": per_page, "offset": (page - 1) * per_page}
            )
        ).mappings().all()
    return GamesPage(games=[dict(row) for row in rows], total=total, page=page, pages=pages)


async def list_platforms() -> list[dict[str, Any]]:
    """Платформы, реально встречающиеся в базе, — варианты для фильтра."""
    async with get_engine().connect() as conn:
        return [dict(row) for row in (await conn.execute(_LIST_PLATFORMS)).mappings().all()]


def normalize_sort(sort: str) -> str:
    return sort if sort in _SORT_SQL else DEFAULT_SORT


def normalize_page(page: int) -> int:
    return page if page >= 1 else 1


# Карточка: `raw` намеренно не выбираем — это полный ответ product на несколько
# сотен килобайт, шаблону он не нужен.
_GET_GAME = text(
    """
    SELECT id, slug, title, description, developer, publisher, esrb_rating,
           release_date, cover_path, video_url, lead_platform_slug,
           best_metascore, best_userscore, genres_cache, first_seen_at, updated_at
    FROM games
    WHERE slug = :slug
    """
)

# Ведущая платформа первой, дальше по убыванию Metascore: в карточке сверху
# должно оказаться то, по чему игру оценивают.
_GAME_PLATFORMS = text(
    """
    SELECT platform_slug, platform_name, is_lead, release_date,
           metascore, metascore_count, metascore_sentiment,
           userscore, userscore_count, userscore_sentiment
    FROM game_platforms
    WHERE game_id = :game_id
    ORDER BY is_lead DESC, metascore DESC NULLS LAST, platform_name
    """
)


# Резюме отзывов (T-30). Две строки на игру максимум — критики и пользователи,
# порядок фиксируем здесь, чтобы шаблон не решал, кто в карточке выше.
_GAME_SUMMARIES = text(
    """
    SELECT audience, platform_slug, liked, disliked, tldr,
           quotes_count, source, status, prompt_version, model, generated_at
    FROM review_summaries
    WHERE game_id = :game_id
    """
)


async def get_review_summaries(game_id: int) -> dict[str, dict[str, Any]]:
    """Резюме по аудиториям: `{"critic": {...}, "user": {...}}`.

    Отсутствующая аудитория — отсутствующий ключ: карточка (T-31) сама решает,
    что показать вместо блока, и отличает «ещё не считали» от `no_data`.
    """
    async with get_engine().connect() as conn:
        rows = (await conn.execute(_GAME_SUMMARIES, {"game_id": game_id})).mappings().all()
    return {row["audience"]: dict(row) for row in rows}


async def get_game(slug: str) -> dict[str, Any] | None:
    """Игра со всеми платформами для `/game/{slug}` или `None`, если её нет."""
    async with get_engine().connect() as conn:
        row = (await conn.execute(_GET_GAME, {"slug": slug})).mappings().first()
        if row is None:
            return None
        platforms = (await conn.execute(_GAME_PLATFORMS, {"game_id": row["id"]})).mappings().all()
    return dict(row) | {"platforms": [dict(p) for p in platforms]}
