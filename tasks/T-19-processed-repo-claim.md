# T-19 — Репозиторий claim'а обработанных игр

Срез: S3. Обязательная часть: да.

## Цель

Реализовать «дедуп = claim»: кандидаты вставляются в `processed_games` до
обработки, `ON CONFLICT (day, game_id) DO NOTHING RETURNING` — это одной
операцией закрывает пересечение New Releases ⊂ SEE ALL и нестабильность
сортировки (design §3.3, RISK #1).

## Файлы

- `app/ingest/processed_repo.py` — реализация `ProcessedRepo` из design §4.2:
  `claim(day, run_id, source, items) -> list[CatalogItem]`,
  `finish(day, game_id, *, ok, error=None) -> None`,
  `counters(day) -> DayCounters`

## Зависимости

T-17.

## Определение готовности

- Вызов `claim` с 20 `CatalogItem`, из которых 5 уже сегодня заклеймлены,
  возвращает ровно 15 новых.
- `finish(..., ok=False, error=...)` переводит статус в `failed` и не даёт
  этой игре быть заклеймленной повторно в тот же день (повторный `claim` с
  тем же `game_id` в тот же день возвращает пусто для неё).
- `counters(day)` даёт `claimed`/`ok`/`failed`, совпадающие с содержимым
  таблицы — используется для восстановления состояния после рестарта (T-36).

## Оценка

40 минут.
