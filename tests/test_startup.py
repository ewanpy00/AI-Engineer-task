"""Старт процесса: недоступная БД не мешает приложению подняться.

Railway поднимает Postgres рядом с приложением и порядок не обещает, а
healthcheck опрашивает `/healthz`. Упавший на `schema.sql` старт отвечать
пробе нечем — платформа видит не «БД недоступна», а приложение, которое не
поднимается вовсе, и разница между этими двумя случаями теряется. Поэтому
здесь проверяется именно то, что процесс встаёт, а отказ виден в `/healthz`.

Lifespan вызывается напрямую: `httpx.ASGITransport` события старта не гоняет,
а проверяется как раз старт.
"""

from __future__ import annotations

import httpx

from app import db
from app.config import get_settings
from app.main import app, lifespan


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_app_starts_when_the_database_is_down(monkeypatch):
    """Схема не накатилась — приложение всё равно поднялось, `/healthz` отдаёт 503."""
    attempts: list[str] = []

    async def unavailable(*_args, **_kwargs):
        attempts.append("try")
        # текст, в котором есть и хост, и порт: он не должен уйти в тело ответа
        raise OSError("connect to user@10.0.0.5:5432 failed")

    monkeypatch.setattr(db, "apply_schema", unavailable)
    monkeypatch.setattr(db, "ping", unavailable)
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False, raising=False)

    async with lifespan(app):  # старт не должен бросить наружу
        async with client() as http:
            response = await http.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": False}
    assert "10.0.0.5" not in response.text  # подробности остаются в логе процесса
    assert attempts  # схему пытались применить, а не пропустили молча


async def test_healthz_is_ok_when_the_database_answers(monkeypatch):
    async def ok() -> bool:
        return True

    monkeypatch.setattr(db, "ping", ok)
    async with client() as http:
        response = await http.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True}
