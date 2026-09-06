"""Выбор батча на один заход (T-18, design §3.3, §4.2).

Первый заход суток идёт по New Releases, все следующие — по SEE ALL со
сдвигом offset. После claim'а часть страницы отсеивается как уже обработанная
сегодня (New Releases ⊂ SEE ALL), поэтому при нехватке игр берётся следующая
страница — но не более пяти за заход, иначе один заход выест дневную выдачу.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.clients.dto import CatalogItem
from app.clients.metacritic import MetacriticClient
from app.ingest.day_cursor_repo import DayCursor, DayCursorRepo, Phase
from app.ingest.processed_repo import ProcessedRepo

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # решение владельца: SEE ALL листается по двадцать позиций


@dataclass(frozen=True)
class Batch:
    """Готовые к обработке игры: уже заклеймлены, дублей внутри быть не может."""

    day: date
    source: Literal["new_releases", "browse"]
    items: list[CatalogItem]
    pages_fetched: int
    cursor_after: DayCursor


class BatchSelector:
    """Реализация `BatchSelector` из design §4.2."""

    def __init__(
        self,
        client: MetacriticClient,
        cursor_repo: DayCursorRepo | None = None,
        processed_repo: ProcessedRepo | None = None,
    ) -> None:
        self._client = client
        self._cursors = cursor_repo or DayCursorRepo()
        self._processed = processed_repo or ProcessedRepo()

    async def next_batch(
        self, day: date, run_id: int, want: int = 20, max_pages: int = 5
    ) -> Batch:
        """Набирает не менее `want` незанятых игр, потратив не более `max_pages` страниц.

        `want` — нижняя граница, а не потолок: последняя страница может добрать
        сверх него, и отдавать эти игры обратно нельзя. Курсор всё равно уйдёт
        за прочитанные позиции (design §3.3), так что «лишняя» игра, снятая с
        claim'а, потерялась бы до завтра.
        """
        cursor = await self._cursors.get_or_create(day)

        if cursor.phase == "exhausted":
            # Каталог за сегодня вычерпан: до смены суток заходы — no-op,
            # ни одного HTTP-вызова.
            # курсор не трогаем вовсе: заход, который ничего не читал, не должен
            # попадать в runs_count дня — сам заход виден в таблице `runs`
            log.info("день %s исчерпан на offset=%s, заход пропущен", day, cursor.browse_offset)
            return Batch(day, "browse", [], 0, cursor)

        if cursor.phase == "new_releases":
            return await self._new_releases(day, run_id, want)
        return await self._browse(day, run_id, want, max_pages, cursor)

    async def _new_releases(self, day: date, run_id: int, want: int) -> Batch:
        """Первый заход суток: одна страница New Releases, дальше день идёт по SEE ALL."""
        items = await self._client.list_new_releases(limit=want)
        claimed = await self._processed.claim(day, run_id, "new_releases", items)
        after = await self._cursors.advance(
            day, phase="browse", browse_offset=0, claimed=len(claimed)
        )
        return Batch(day, "new_releases", claimed, 1, after)

    async def _browse(
        self, day: date, run_id: int, want: int, max_pages: int, cursor: DayCursor
    ) -> Batch:
        collected: list[CatalogItem] = []
        offset = cursor.browse_offset
        phase: Phase = "browse"
        pages = 0

        while len(collected) < want and pages < max_pages:
            page = await self._client.list_browse(offset, limit=PAGE_SIZE)
            pages += 1
            offset += PAGE_SIZE  # сдвигаем на запрошенное, а не на полученное:
            # выпавшие из маппинга позиции всё равно прочитаны
            collected += await self._processed.claim(day, run_id, "browse", page.items)

            if not page.items or (page.total and offset >= page.total):
                phase = "exhausted"
                break

        after = await self._cursors.advance(
            day, phase=phase, browse_offset=offset, claimed=len(collected)
        )
        return Batch(day, "browse", collected, pages, after)
