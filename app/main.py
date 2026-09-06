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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.apply_schema()
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
    try:
        ok = await db.ping()
    except Exception as exc:  # noqa: BLE001 — healthz не должен падать 500 без тела
        return JSONResponse({"status": "error", "db": False, "error": str(exc)}, 503)
    return JSONResponse({"status": "ok", "db": ok})
