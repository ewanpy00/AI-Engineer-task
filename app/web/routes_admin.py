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
    """Один общий секрет на всю админку (design §8, OQ-8).

    Сравнение идёт по байтам, а не по строкам: заголовки Starlette декодирует
    как latin-1, и `compare_digest` на строке с символом за пределами ASCII
    бросает `TypeError` — ручка отвечала бы 500 вместо 401 и отличалась бы по
    коду ответа от обычного промаха.

    Пустой `expected` (ADMIN_TOKEN не задан) закрывает ручку целиком: это
    незаполненная конфигурация, а не разрешение всем.
    """
    expected = get_settings().admin_token
    if not expected or not token:
        raise HTTPException(status_code=401, detail="invalid admin token")
    if not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
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
