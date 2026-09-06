"""Служебные ручки: ручной запуск обхода (T-23).

Кнопка и планировщик дёргают одну и ту же корутину `IngestRunner.run`,
отличается только `trigger`. Ручка отвечает сразу: заход на двадцать игр —
это минуты, держать HTTP-соединение всё это время незачем.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.ingest.runner import get_runner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Ссылки на фоновые заходы: без них event loop имеет право собрать задачу
# сборщиком мусора прямо посреди обхода.
_background: set[asyncio.Task] = set()


def require_admin(token: str | None) -> None:
    """Один общий секрет на всю админку (design §8, OQ-8)."""
    expected = get_settings().admin_token
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


@router.post("/run", status_code=202)
async def run(x_admin_token: str | None = Header(default=None)) -> JSONResponse:
    """Ставит заход в фон. 409, если обход уже идёт."""
    require_admin(x_admin_token)
    runner = get_runner()
    if await runner.is_locked():
        raise HTTPException(status_code=409, detail="ingest already running")

    task = asyncio.create_task(runner.run("manual"))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return JSONResponse({"status": "accepted", "trigger": "manual"}, status_code=202)
