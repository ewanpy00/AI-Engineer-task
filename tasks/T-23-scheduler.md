# T-23 — Планировщик APScheduler + ручной запуск через runner

Срез: S3. Обязательная часть: да.

## Цель

Запускать `IngestRunner.run('schedule')` раз в час автоматически (ADR-2), и
дать `POST /admin/run` запускать ту же корутину в фоне с `trigger='manual'`.

## Файлы

- `app/scheduler.py` — `AsyncIOScheduler`, cron `hour='*', minute=0,
  timezone=UTC`, `max_instances=1`, `coalesce=True`,
  `misfire_grace_time=600`; старт/остановка на lifecycle FastAPI-приложения
- `app/web/routes_admin.py` — `POST /admin/run` вызывает
  `asyncio.create_task(runner.run('manual'))`, возвращает 202 сразу
  (без ожидания завершения); 409, если `advisory lock` уже занят —
  это отдельная быстрая проверка перед постановкой в фон, детали см. T-39
  (там же навешивается admin-токен)

## Зависимости

T-22.

## Определение готовности

- После деплоя сервис сам добирает новые игры каждый час без ручных действий
  (проверяется по новым строкам в `runs` с `trigger='schedule'`).
- `POST /admin/run` во время уже идущего захода отвечает 409 и не запускает
  второй `IngestRunner.run`.
- Рестарт процесса не приводит к двум одновременным запускам в один и тот же
  час (advisory lock из T-22 это гарантирует независимо от планировщика).

## Оценка

40 минут.
