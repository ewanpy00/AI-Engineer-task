# T-24 — Gemini-клиент: адаптер structured output

Срез: S4. Обязательная часть: да.

**Важно:** по `11-decisions.md` (OQ-1 + «Поправка к ADR-7») LLM-провайдер —
Google AI Studio (Gemini), **не** Anthropic из `docs/10-design.md` §5/ADR-7.
Структурированный вывод — через `responseSchema`, не через tool use.
Контракт `LlmClient`/`LlmResult` из design §4.4 сохраняется как форма,
меняется только реализация.

## Цель

Реализовать единственный адаптер к LLM, на который опираются T-30 (резюме
отзывов) и T-43 (заключение по летсплею).

## Файлы

- `app/llm/gemini_client.py` — реализация `LlmClient` из design §4.4:
  `summarize_reviews(...)`, `conclude_letsplay(...)`, через
  `google-genai` SDK, `responseSchema` = JSON-схема, сгенерированная из
  Pydantic-модели (`ReviewSummaryOut`, `LetsplayConclusionOut`)
  - модель — строка из конфига (`GEMINI_MODEL`, значение по умолчанию
    `gemini-3.8-flash` по решению владельца)
  - ретрай на 429 с backoff (2/4/8 с), до 3 попыток — по аналогии с
    политикой design §5.5, но коды ошибок не Anthropic-специфичные
  - возвращает `LlmResult` с `ok`, `value`, `error`, `prompt_version`,
    `model`, `input_tokens`/`output_tokens` (если SDK их отдаёт), `latency_ms`

## Зависимости

T-01.

## Определение готовности

- Вызов `summarize_reviews` с тестовым набором цитат возвращает
  `LlmResult.ok=True` и `value` — валидный `ReviewSummaryOut` (2..5 `liked`,
  0..5 `disliked`, `tldr` ≤ 300 символов) — без ручного парсинга JSON.
- Принудительный 429 (или мок) вызывает ровно предусмотренное число ретраев,
  затем `ok=False`, `error` заполнен, исключение наружу не летит.
- **ASSUMPTION, зафиксировать в коде комментарием:** если `GEMINI_MODEL`
  из конфига недоступен в API на момент реализации, меняется только значение
  константы/env, а не архитектура адаптера — модель не проверялась на
  research-стадии.

## Оценка

55 минут.
