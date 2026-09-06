# T-18 — Репозиторий курсора дня

Срез: S3. Обязательная часть: да.

## Цель

Реализовать «сброс раз в сутки без cron-джобы»: курсор ключуется UTC-датой,
первый заход новых суток создаёт строку через `INSERT ... ON CONFLICT (day)
DO NOTHING RETURNING *` (design §3.3, ADR-6).

## Файлы

- `app/ingest/day_cursor_repo.py` — реализация `DayCursorRepo` из design
  §4.2: `get_or_create(day) -> DayCursor`, `advance(day, *, phase,
  browse_offset, claimed) -> DayCursor`

## Зависимости

T-17.

## Определение готовности

- Первый вызов `get_or_create(today)` создаёт строку с
  `phase='new_releases'`, `browse_offset=0`.
- Два одновременных вызова `get_or_create(today)` (гонка) не создают вторую
  строку и не падают (используется `ON CONFLICT DO NOTHING` + чтение).
- `advance(...)` обновляет `phase`/`browse_offset`/`claimed_count` и
  `updated_at`, `runs_count` увеличивается на 1 за вызов.

## Оценка

35 минут.
