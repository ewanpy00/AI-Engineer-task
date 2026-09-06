"""Похожие игры: top-5 из своей же базы (design §3.5).

Считается на лету при открытии карточки — ни кэша, ни материализованной вьюхи
(ADR-4). Никаких обращений к Metacritic: похожесть строится только по тем
играм, которые обход уже сложил в базу.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.db import get_engine

LIMIT = 5

# Запрос из design §3.5. Вес разработчика — +2 «виртуальных жанра»: совпадение
# студии сильнее одного общего жанра, но слабее трёх. Число зафиксировано
# дизайном и не конфигурируется.
#
# Игра без жанров даёт пустой массив в `me.genres`, `ANY (…)` не совпадает ни с
# одной строкой, и результат пуст — отдельной ветки для этого случая не нужно.
_SIMILAR = text(
    """
    WITH me AS (
        SELECT g.id, g.developer,
               ARRAY(SELECT genre FROM game_genres WHERE game_id = g.id) AS genres
        FROM games g WHERE g.id = :game_id
    )
    SELECT o.id, o.slug, o.title, o.cover_path, o.best_metascore,
           count(og.genre) AS shared_genres,
           count(og.genre) + (CASE WHEN o.developer IS NOT NULL
                                    AND o.developer = me.developer THEN 2 ELSE 0 END) AS score
    FROM me
    JOIN game_genres og ON og.genre = ANY (me.genres)
    JOIN games o        ON o.id = og.game_id AND o.id <> me.id
    GROUP BY o.id, o.slug, o.title, o.cover_path, o.best_metascore, me.developer, o.developer
    ORDER BY score DESC, o.best_metascore DESC NULLS LAST, o.title
    LIMIT :limit
    """
)


async def list_similar(game_id: int, limit: int = LIMIT) -> list[dict[str, Any]]:
    """До `limit` похожих игр, самые близкие первыми.

    Пустой список — нормальный ответ: у игры нет жанров, база ещё мала или
    пересечений не нашлось. Несуществующий `game_id` тоже даёт пустой список,
    а не ошибку: карточка сама решает, что показать.
    """
    async with get_engine().connect() as conn:
        rows = (await conn.execute(_SIMILAR, {"game_id": game_id, "limit": limit})).mappings().all()
    return [dict(row) for row in rows]
