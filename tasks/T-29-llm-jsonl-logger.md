# T-29 — JSONL-логгер LLM-вызовов

Срез: S4. Обязательная часть: да.

## Цель

Реализовать обязательное по правилам проекта логирование каждого обращения к
LLM: `ts, prompt_version, model, messages, response, tokens, latency_ms` —
одной строкой в `logs/llm/*.jsonl` (CLAUDE.md; design §5.4).

## Файлы

- `app/llm/jsonl_logger.py` — реализация `JsonlLogger` из design §4.4:
  `write(record: dict) -> None`, файл `logs/llm/{YYYY-MM-DD}.jsonl`,
  append-only, запись под `asyncio.Lock`, вызывается **после** ответа
  (успех или отказ)

## Зависимости

T-01.

## Определение готовности

- После вызова (успешного или упавшего) в `logs/llm/{сегодня}.jsonl`
  появляется ровно одна валидная JSON-строка с полями `ts`, `prompt_version`,
  `prompt_sha256`, `model`, `messages` (записаны **полностью, как отправлены**,
  без усечения), `response`, `tokens`, `latency_ms`, `status`, `error`,
  `attempt`.
- Каждая попытка ретрая (T-24/T-30) пишет свою отдельную строку.
- Параллельные записи (несколько игр одновременно, `Semaphore(4)` из T-22) не
  перемежают и не рвут JSON-строки друг друга.

## Оценка

35 минут.
