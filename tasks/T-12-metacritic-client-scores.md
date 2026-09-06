# T-12 — Metacritic client: userscore по платформам

Срез: S2. Обязательная часть: да.

## Цель

Добрать Userscore для каждой платформы игры (Metascore уже приходит из
`get_product`, T-07). По решению владельца: Userscore — по всем платформам
игры, отзывы (сводки/цитаты) — только по ведущей (это в T-27).

## Файлы

- `app/clients/metacritic.py` — `get_score_stats(slug, platform_slug,
  audience="user") -> ScoreStats | None`
- `app/clients/dto.py` — `ScoreStats` (score, count, sentiment)

## Зависимости

T-05.

## Определение готовности

- Для игры с N платформами метод вызывается N раз (по одному на платформу) и
  возвращает `ScoreStats` либо `None`, если оценок нет.
- Проверено вручную на игре с несколькими платформами (например, PS5 + PC).

## Оценка

35 минут.
