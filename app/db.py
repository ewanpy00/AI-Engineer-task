"""Async-подключение к Postgres и применение schema.sql при старте.

Схема накатывается напрямую из файла (ADR-3): без Alembic и без
`Base.metadata.create_all`. Запросы — Core/raw SQL.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def apply_schema() -> None:
    """Идемпотентно применяет schema.sql. Повторный запуск ничего не ломает.

    Файл содержит несколько команд, а asyncpg отказывается класть их в один
    prepared statement, поэтому идём напрямую в драйвер: `Connection.execute`
    без параметров использует simple query protocol и мультикоманды разрешает.
    """
    ddl = get_settings().schema_path.read_text(encoding="utf-8")
    async with get_engine().begin() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.execute(ddl)


async def ping() -> bool:
    async with get_engine().connect() as conn:
        return (await conn.scalar(text("SELECT 1"))) == 1


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
