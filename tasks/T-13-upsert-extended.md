# T-13 — Upsert: userscore, ESRB, описание, видео, lead-платформа

Срез: S2. Обязательная часть: да.

## Цель

Дополнить `upsert_game` из T-08 полями, которые ещё не сохранялись:
`userscore`/`userscore_count`/`userscore_sentiment` на `game_platforms`,
`description`, `esrb_rating`, `video_url`, `lead_platform_slug`,
`best_userscore` на `games`.

## Файлы

- `app/ingest/upsert.py` — расширение той же транзакции: цикл по платформам
  игры с вызовом `get_score_stats` (T-12) для каждой, запись в
  `game_platforms.userscore*`; `best_userscore` = максимум по платформам

## Зависимости

T-08, T-12.

## Определение готовности

- После upsert у игры с несколькими платформами в БД заполнены
  `userscore`/`userscore_count` для каждой платформы, где они были у API.
- `games.best_userscore`, `games.description`, `games.video_url`,
  `games.esrb_rating`, `games.lead_platform_slug` заполнены (или `NULL`,
  если источник их не дал).

## Оценка

40 минут.
