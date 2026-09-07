"""FastAPI-приложение: серверный рендеринг Jinja2, без SPA."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.config import get_settings
from app.ingest.runner import close_runner, get_runner
from app.scheduler import create_scheduler
from app.state import restore as restore_state
from app.web import routes_admin, routes_games, routes_status
from app.web.templating import templates  # noqa: F401  (инициализация Jinja2-окружения)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def log_effective_config() -> None:
    """Что реально включено в этом процессе — первой строкой в логе.

    Модель, окружение и рубильники задаются переменными окружения, и на проде
    их значение расходится с дефолтом репозитория незаметно: заход при этом
    отрабатывает «успешно», а резюме не появляются. Одна строка в старте
    отвечает на вопрос «какая модель у прода» без доступа к БД. Значения
    секретов не пишем — только факт, задан ли секрет.
    """
    s = get_settings()
    log.info(
        "конфигурация: модель %s, LLM %s (ключ %s); летсплеи %s (кука 300.ya.ru %s); "
        "планировщик %s",
        s.gemini_model,
        "включён" if s.llm_enabled else "ВЫКЛЮЧЕН",
        "задан" if s.google_api_key else "НЕ ЗАДАН",
        "включены" if s.letsplay_enabled else "ВЫКЛЮЧЕНЫ",
        "задана" if s.ya300_session_id else "НЕ ЗАДАНА",
        "включён" if s.scheduler_enabled else "выключен",
    )


async def bootstrap_schema() -> None:
    """Накатывает schema.sql, но старт процесса на этом не завязан.

    Недоступная на старте БД (Railway поднимает Postgres рядом с приложением,
    и порядок не гарантирован) не должна валить процесс: упавший старт
    healthcheck'у отвечать нечем, и вместо «БД недоступна» платформа видит
    приложение, которое не поднимается вовсе. Поднимаемся всегда, а факт
    отказа отдаёт `/healthz` — 503, пока `SELECT 1` не проходит.

    Схема идемпотентна, поэтому следующий старт применит её заново. Пока она
    не применена, заходы будут падать на отсутствующих таблицах — с честной
    строкой в `runs.error` и в логе процесса.
    """
    try:
        await db.apply_schema()
    except Exception:  # noqa: BLE001 — старт важнее схемы, см. docstring
        log.exception("schema.sql не применён: база недоступна, /healthz отдаёт 503")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_effective_config()
    await bootstrap_schema()
    # События рестарт не переживают, дневные счётчики — обязаны (T-35).
    await restore_state()
    scheduler = create_scheduler(get_runner()) if get_settings().scheduler_enabled else None
    if scheduler is not None:
        scheduler.start()
        log.info("планировщик запущен: cron 0 * * * * UTC")
    try:
        yield
    finally:
        if scheduler is not None:
            # wait=False: идущий заход всё равно оборвётся вместе с процессом,
            # claim за ним останется — переобработка будет завтра (ADR-6)
            scheduler.shutdown(wait=False)
        await close_runner()
        await db.dispose_engine()


app = FastAPI(title="Metacritic digest", lifespan=lifespan)
app.include_router(routes_games.router)
app.include_router(routes_admin.router)
app.include_router(routes_status.router)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Проба для healthcheck'а Railway. Ручка открыта всем, поэтому в теле —
    только факт отказа: текст исключения asyncpg/SQLAlchemy содержит хост, порт
    и пользователя БД, а иногда и строку подключения целиком. Подробности
    уходят в лог процесса."""
    try:
        ok = await db.ping()
    except Exception:  # noqa: BLE001 — healthz не должен падать 500 без тела
        log.exception("healthz: база недоступна")
        return JSONResponse({"status": "error", "db": False}, 503)
    return JSONResponse({"status": "ok", "db": ok})
