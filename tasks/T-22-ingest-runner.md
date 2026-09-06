# T-22 — Ingest runner (advisory lock, оркестрация захода)

Срез: S3. Обязательная часть: да.

## Цель

Собрать один часовой заход целиком: advisory lock, фиксация `day` на весь
заход, вызов `BatchSelector`, обработка каждой игры, запись в `runs`. Пока
без LLM (T-32), похожих игр (не нужны здесь), событий (T-37) и летсплея
(T-45) — они подключаются позже как best-effort шаги внутри
`process_game`.

## Файлы

- `app/ingest/runner.py` — `IngestRunner.run(trigger) -> RunResult`,
  `process_game(item, run_id) -> GameResult` по контракту design §4.3
  - `pg_try_advisory_lock`, при неудаче — статус `skipped_locked`, лок
    снимается в `finally`
  - `Semaphore(4)` на конкурентную обработку игр внутри захода (поверх
    глобального rate-limit клиента из T-05)
  - на каждую игру: `get_product` (T-07) + userscore (T-12) → `upsert_game`
    (T-13) → `processed_repo.finish(ok=True/False)`
  - запись в `runs`: `pages_fetched`, `games_claimed`, `games_ok`,
    `games_failed`, `started_at`/`finished_at`

## Зависимости

T-20, T-21, T-08.

## Определение готовности

- Два параллельных вызова `run()` — второй завершается со статусом
  `skipped_locked`, не трогая БД игр.
- Одна упавшая игра (например, `get_product` кинул `MetacriticError`) не
  прерывает обработку остальных игр батча; `processed_games.status='failed'`
  и `error` заполнен для неё.
- После штатного захода в `runs` — одна новая строка с корректными
  счётчиками, `status='ok'` (или `'empty'`, если `Batch.items` пуст).
- Ручка `POST /admin/run` из T-09 заменена на вызов `IngestRunner.run
  (trigger='manual')`.

## Оценка

55 минут.
