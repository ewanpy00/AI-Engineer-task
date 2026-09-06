"""FastAPI-приложение: серверный рендеринг Jinja2, без SPA."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.web import routes_games
from app.web.templating import templates  # noqa: F401  (инициализация Jinja2-окружения)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.apply_schema()
    yield
    await db.dispose_engine()


app = FastAPI(title="Metacritic digest", lifespan=lifespan)
app.include_router(routes_games.router)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        ok = await db.ping()
    except Exception as exc:  # noqa: BLE001 — healthz не должен падать 500 без тела
        return JSONResponse({"status": "error", "db": False, "error": str(exc)}, 503)
    return JSONResponse({"status": "ok", "db": ok})
