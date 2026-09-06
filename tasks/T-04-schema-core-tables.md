# T-04 — Схема: games / game_platforms / game_genres

Срез: S1. Обязательная часть: да.

## Цель

Добавить в `schema.sql` таблицы для хранения игр, платформ и жанров ровно по
DDL из `docs/10-design.md` §3.1.

## Файлы

- `schema.sql` — таблицы `games`, `game_platforms`, `game_genres` и индексы
  (`games_title_trgm_idx`, `games_best_metascore_idx`, `games_best_userscore_idx`,
  `games_first_seen_idx`, `game_platforms_slug_idx`, `game_genres_genre_idx`)

## Зависимости

T-02.

## Определение готовности

- После рестарта приложения (T-02 применяет schema.sql) в БД существуют все
  три таблицы и все перечисленные индексы (проверяется `\d games` /
  `\di` в psql).
- Повторное применение schema.sql не падает и не дублирует индексы.

## Оценка

30 минут.
