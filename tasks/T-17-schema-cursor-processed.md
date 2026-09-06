# T-17 — Схема: day_cursor / processed_games

Срез: S3. Обязательная часть: да.

## Цель

Добавить в `schema.sql` таблицы состояния часового обхода — курсор дня и
журнал claim'ов — ровно по DDL из `docs/10-design.md` §3.3.

## Файлы

- `schema.sql` — таблицы `day_cursor`, `processed_games` и индексы
  (`processed_games_day_status_idx`, `processed_games_run_idx`)

## Зависимости

T-02.

## Определение готовности

- Таблицы и индексы существуют после применения schema.sql.
- `day_cursor.day` — `PRIMARY KEY`; `processed_games` — `PRIMARY KEY
  (day, game_id)`, `FK` на `games` сознательно отсутствует (клеймим до
  вставки в `games` — комментарий в SQL об этом присутствует).

## Оценка

25 минут.
