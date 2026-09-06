"""Оркестратор одного захода (T-22, design §2.3, §4.3).

Заход = advisory lock + фиксированный день + батч из селектора + обработка игр
+ строка в `runs`. Ни LLM-резюме (T-32), ни похожих игр, ни событий (T-37), ни
летсплея (T-45) здесь пока нет: они подключаются внутрь `process_game`
отдельными best-effort шагами.

День фиксируется один раз на весь заход: обход, начавшийся в 23:59, должен
целиком лечь в свои сутки, иначе claim и курсор разъедутся.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import AsyncIterator, Literal

from sqlalchemy import text

from app.clients.dto import CatalogItem, Product, ScoreStats
from app.clients.metacritic import MetacriticClient
from app.db import get_engine
from app.ingest.day_cursor_repo import DayCursorRepo
from app.ingest.processed_repo import ProcessedRepo
from app.ingest.selector import BatchSelector
from app.ingest.upsert import upsert_game

log = logging.getLogger(__name__)

Trigger = Literal["schedule", "manual"]
RunStatus = Literal["ok", "empty", "failed", "skipped_locked"]

CONCURRENCY = 4  # поверх глобального лимита клиента в 1 rps: параллелизм скрывает задержки БД
ERROR_LIMIT = 1000

# Ключ advisory-локи: детерминированный bigint из имени, чтобы не пересечься
# с чужими локами в той же базе.
LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"metacritic-digest:ingest").digest()[:8], "big", signed=True
)


@dataclass(frozen=True)
class GameResult:
    item: CatalogItem
    ok: bool
    game_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunResult:
    day: date
    trigger: Trigger
    status: RunStatus
    run_id: int | None = None
    phase: str | None = None
    pages_fetched: int = 0
    games_claimed: int = 0
    games_ok: int = 0
    games_failed: int = 0
    error: str | None = None


_INSERT_RUN = text(
    "INSERT INTO runs (day, trigger, status) VALUES (:day, :trigger, 'running') RETURNING id"
)
_INSERT_SKIPPED = text(
    "INSERT INTO runs (day, trigger, status, finished_at) "
    "VALUES (:day, :trigger, 'skipped_locked', now())"
)
_UPDATE_BATCH = text(
    "UPDATE runs SET phase = :phase, pages_fetched = :pages, games_claimed = :claimed "
    "WHERE id = :id"
)
_FINISH_RUN = text(
    """
    UPDATE runs SET
        status = :status, phase = :phase, pages_fetched = :pages,
        games_claimed = :claimed, games_ok = :ok, games_failed = :failed,
        finished_at = now(), error = :error
    WHERE id = :id
    """
)
_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")


@asynccontextmanager
async def advisory_lock(key: int = LOCK_KEY) -> AsyncIterator[bool]:
    """Держит сессионную advisory-локу на отдельном соединении.

    Соединение нужно своё и на весь заход: лока живёт в сессии Postgres, а
    соединение из пула вернулось бы туда сразу после запроса. AUTOCOMMIT —
    чтобы не таскать за собой открытую транзакцию всё время обхода.
    """
    acquired = False
    conn = await get_engine().connect()
    try:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(await conn.scalar(_TRY_LOCK, {"key": key}))
        yield acquired
    finally:
        try:
            if acquired:
                await conn.scalar(_UNLOCK, {"key": key})
        finally:
            await conn.close()


async def fetch_user_scores(
    client: MetacriticClient, product: Product
) -> dict[str, ScoreStats | None]:
    """Userscore по каждой платформе игры — отдельный вызов на платформу.

    Решение владельца: Userscore по всем платформам, отзывы — только по ведущей.
    """
    return {
        platform.slug: await client.get_score_stats(product.slug, platform.slug, "user")
        for platform in product.platforms
    }


class IngestRunner:
    """Реализация `IngestRunner` из design §4.3.

    Живёт на всё время процесса: и планировщик (T-23), и ручной запуск дёргают
    один и тот же экземпляр, поэтому глобальный лимит в 1 rps общий на все
    заходы, а не на каждый свой.
    """

    def __init__(
        self,
        client: MetacriticClient | None = None,
        cursor_repo: DayCursorRepo | None = None,
        processed_repo: ProcessedRepo | None = None,
        *,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self._client = client
        self._cursors = cursor_repo or DayCursorRepo()
        self._processed = processed_repo or ProcessedRepo()
        self._concurrency = concurrency

    @property
    def client(self) -> MetacriticClient:
        if self._client is None:
            self._client = MetacriticClient()
        return self._client

    async def run(self, trigger: Trigger = "schedule") -> RunResult:
        day = datetime.now(UTC).date()
        async with advisory_lock() as acquired:
            if not acquired:
                log.info("заход %s пропущен: обход уже идёт", trigger)
                async with get_engine().begin() as conn:
                    await conn.execute(_INSERT_SKIPPED, {"day": day, "trigger": trigger})
                return RunResult(day=day, trigger=trigger, status="skipped_locked")
            return await self._run_locked(day, trigger)

    async def _run_locked(self, day: date, trigger: Trigger) -> RunResult:
        async with get_engine().begin() as conn:
            run_id = int(await conn.scalar(_INSERT_RUN, {"day": day, "trigger": trigger}))
        log.info("заход #%s (%s) за %s начат", run_id, trigger, day)

        phase: str | None = None
        pages = claimed = ok = failed = 0
        status: RunStatus = "failed"
        error: str | None = None
        try:
            selector = BatchSelector(self.client, self._cursors, self._processed)
            batch = await selector.next_batch(day, run_id)
            # в `runs.phase` пишем фазу дня *после* захода: странице статуса важно,
            # откуда пойдёт следующий час, а не откуда пришёл этот батч
            phase, pages, claimed = batch.cursor_after.phase, batch.pages_fetched, len(batch.items)
            async with get_engine().begin() as conn:
                await conn.execute(
                    _UPDATE_BATCH,
                    {"id": run_id, "phase": phase, "pages": pages, "claimed": claimed},
                )

            results = await self._process_all(batch.items, run_id, day)
            ok = sum(1 for r in results if r.ok)
            failed = len(results) - ok
            status = "ok" if batch.items else "empty"
        except Exception as exc:  # noqa: BLE001 — заход не должен уронить процесс
            error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
            log.exception("заход #%s провалился", run_id)
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    _FINISH_RUN,
                    {
                        "id": run_id, "status": status, "phase": phase, "pages": pages,
                        "claimed": claimed, "ok": ok, "failed": failed, "error": error,
                    },
                )

        log.info("заход #%s завершён: %s, ok=%s failed=%s", run_id, status, ok, failed)
        return RunResult(
            day=day, trigger=trigger, status=status, run_id=run_id, phase=phase,
            pages_fetched=pages, games_claimed=claimed, games_ok=ok, games_failed=failed,
            error=error,
        )

    async def _process_all(
        self, items: list[CatalogItem], run_id: int, day: date
    ) -> list[GameResult]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded(item: CatalogItem) -> GameResult:
            async with semaphore:
                return await self.process_game(item, run_id, day)

        return list(await asyncio.gather(*(guarded(item) for item in items)))

    async def process_game(self, item: CatalogItem, run_id: int, day: date) -> GameResult:
        """Обрабатывает одну игру целиком. Наружу не бросает — исход в `GameResult`.

        `day` передаётся явно (отклонение от сигнатуры design §4.3): брать его
        из часов внутри значит рискнуть тем, что игра заклеймлена вчера, а
        `finish` уедет в сегодня и статус останется висеть.
        """
        try:
            product = await self.client.get_product(item.slug)
            user_scores = await fetch_user_scores(self.client, product)
            game_id = await upsert_game(product, user_scores)
        except Exception as exc:  # noqa: BLE001 — одна игра не роняет заход
            error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
            log.warning("игра %s провалилась: %s", item.slug, error)
            await self._processed.finish(day, item.id, ok=False, error=error)
            return GameResult(item=item, ok=False, error=error)

        await self._processed.finish(day, item.id, ok=True)
        return GameResult(item=item, ok=True, game_id=game_id)

    async def is_locked(self) -> bool:
        """Быстрая проверка «заход уже идёт» для ответа 409 (T-23).

        Ровно проверка, а не резервирование: между ней и стартом фоновой задачи
        успел бы влезть плановый заход. Настоящая гарантия — лока внутри `run`,
        второй заход выйдет со статусом `skipped_locked`.
        """
        async with advisory_lock() as acquired:
            return not acquired

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


_runner: IngestRunner | None = None


def get_runner() -> IngestRunner:
    """Один runner на процесс: общий HTTP-клиент и общий rate-limit."""
    global _runner
    if _runner is None:
        _runner = IngestRunner()
    return _runner


async def close_runner() -> None:
    global _runner
    if _runner is not None:
        await _runner.aclose()
    _runner = None
