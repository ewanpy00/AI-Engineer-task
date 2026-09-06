# T-28 — Схема: review_summaries

Срез: S4. Обязательная часть: да.

## Цель

Таблица для хранения результата резюмирования отзывов, включая `quotes_hash`
для пропуска повторного вызова LLM (design §3.2).

## Файлы

- `schema.sql` — таблица `review_summaries` (PK `(game_id, audience)`)

## Зависимости

T-02.

## Определение готовности

- Таблица существует, `audience` ограничен `critic|user`, `status` —
  `ok|no_data|llm_failed`, `source` — `summary_endpoint|review_list`.
- `PRIMARY KEY (game_id, audience)` — повторная запись по той же паре
  обновляет строку, не создаёт вторую.

## Оценка

30 минут.
