# T-44 — Пайплайн летсплея (find → retell → conclude, статусы)

Срез: S7. Обязательная часть: нет (доп. часть 1).

## Цель

Собрать T-41/T-42/T-43 в одну best-effort операцию на игру с чёткими
статусами в БД (ADR-8: фича обязана деградировать, а не падать).

## Файлы

- `app/letsplay/pipeline.py` — реализация `LetsplayPipeline.enrich(game_id,
  title) -> LetsplayStatus` из design §4.5:
  1. `finder.find(title)` → нет результата → `status='not_found'`, запись,
     выход
  2. `retelling.retell(video_url)` → `RetellingUnavailable` →
     `status='service_error'`, `error` заполнен, выход
  3. `conclude_letsplay(...)` → результат в `letsplays.conclusion`
  4. `status='ok'`, `attempts += 1`, `last_attempt_at = now()`
  - одна попытка на игру за обход (без внутренних ретраев пайплайна,
    сами адаптеры уже best-effort)

## Зависимости

T-40, T-41, T-42, T-43.

## Определение готовности

- На реальной популярной игре пайплайн доходит до `status='ok'` с
  заполненными `video_url`, `retelling`, `conclusion`.
- Отключение `YA300_SESSION_ID` (пустое значение) даёт `status='service_error'`
  для всех игр предсказуемо, без падения пайплайна.
- Любое исключение внутри `enrich` перехватывается на уровне пайплайна и не
  пробрасывается наружу — вызывающая сторона (T-45) не обязана оборачивать
  каждый вызов в `try/except`.

## Оценка

50 минут.
