# T-01 — Скелет проекта и конфиг

Срез: S0. Обязательная часть: да.

## Цель

Создать структуру Python-проекта и единый способ читать конфиг из переменных
окружения, на который будут опираться все последующие задачи.

## Файлы

- `pyproject.toml` (или `requirements.txt`) — FastAPI, SQLAlchemy (async),
  asyncpg, APScheduler, jinja2, httpx, python-dotenv, google-genai (клиент
  Gemini, см. T-24), yt-dlp (см. T-41), pydantic v2
- `app/__init__.py`
- `app/config.py` — чтение env через pydantic `BaseSettings`
- `.env.example` — список всех переменных с комментариями
- `app/web/`, `app/ingest/`, `app/clients/`, `app/llm/`, `app/letsplay/` —
  пустые пакеты (`__init__.py`), по номенклатуре модулей из design §2.1

## Зависимости

Нет.

## Определение готовности

- `python -m app` (или аналог) импортирует `app.config.get_settings()` без
  ошибок при наличии `.env`, скопированного из `.env.example`.
- В `.env.example` перечислены как минимум: `DATABASE_URL`, `ADMIN_TOKEN`,
  `GOOGLE_API_KEY`, `YA300_SESSION_ID`, `METACRITIC_BASE_URL`.
- Структура пакетов совпадает с таблицей модулей из `docs/10-design.md` §2.1.

## Оценка

40 минут.
