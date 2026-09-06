"""Служебные ручки. Пока только ручной запуск обработки (T-09)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.ingest.pipeline import run_minimal_ingest

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(token: str | None) -> None:
    """Один общий секрет на всю админку (design §8, OQ-8)."""
    expected = get_settings().admin_token
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


# TODO T-22/T-23/T-39: заменить на запуск runner'а в фоне (202 + 409, если заход идёт)
@router.post("/run")
async def run(
    limit: int = Query(20, ge=1, le=50),
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    require_admin(x_admin_token)
    return JSONResponse(await run_minimal_ingest(limit))
