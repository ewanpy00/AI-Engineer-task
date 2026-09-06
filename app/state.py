"""Состояние воркера в памяти (T-35, design §4.3).

Страница статуса рисуется из этого объекта, а не из БД: за время часового
захода состояние меняется десятки раз, и гонять на каждый чих запрос в
Postgres незачем.

Единственный источник изменений — события шины: `apply` вызывается ровно там,
где событие публикуется (`IngestRunner._emit`), поэтому лента и счётчики не
могут разъехаться между собой.

После рестарта в памяти пусто, а `logs`-буфер и подавно. Агрегаты за сегодня
восстанавливаются из `processed_games` и `runs` (`restore`) — решение
владельца: «счётчики после рестарта восстанавливаем из журнала обработанных
игр». Событий за прошлое время не восстанавливаем: их негде взять, лента
начинается с чистого листа.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import text

from app.db import get_engine
from app.events import Event
from app.ingest.day_cursor_repo import DayCursorRepo
from app.ingest.processed_repo import ProcessedRepo

log = logging.getLogger(__name__)

_LLM_TOTALS = text(
    "SELECT coalesce(sum(llm_calls), 0) AS calls, coalesce(sum(llm_failures), 0) AS failures "
    "FROM runs WHERE day = :day"
)


def today() -> date:
    """UTC-сутки: день у всего проекта один и тот же (decisions, «день считаем по UTC»)."""
    return datetime.now(UTC).date()


@dataclass
class WorkerState:
    """`WorkerState` из design §4.3.

    Счётчики — дневные, а не «за последний заход»: и восстановление из БД, и
    вопрос «сколько сегодня сделано» работают именно так. Смена суток обнуляет
    их сама, на первом событии нового дня.

    Отличий от контракта два. `current_game` — вычисляемое: обход идёт с
    параллелизмом 4 (T-22), одной «текущей игры» в природе не существует,
    поэтому в памяти лежит весь набор игр в работе, а полем притворяется первая
    из них. И добавлены `llm_calls`/`llm_failures`: без них на странице статуса
    не видно деградации LLM (design §5.5), а восстанавливаются они тем же
    запросом к `runs`, что и остальное.
    """

    status: Literal["idle", "running"] = "idle"
    run_id: int | None = None
    day: date = field(default_factory=today)
    phase: str = "new_releases"
    browse_offset: int = 0
    claimed: int = 0
    ok: int = 0
    failed: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    started_at: datetime | None = None
    last_event_at: datetime | None = None
    trigger: str | None = None
    in_flight: dict[str, str] = field(default_factory=dict)  # slug -> title

    @property
    def current_game(self) -> str | None:
        return next(iter(self.in_flight.values()), None)

    @property
    def in_progress(self) -> int:
        """Заклеймлено, но ещё не доведено до `ok`/`failed`."""
        return max(self.claimed - self.ok - self.failed, 0)

    def roll_day(self, day: date) -> None:
        """Новые сутки — новые счётчики. Без отдельной джобы, как и курсор дня."""
        if day == self.day:
            return
        self.day, self.claimed, self.ok, self.failed = day, 0, 0, 0
        self.llm_calls = self.llm_failures = 0
        self.browse_offset, self.phase = 0, "new_releases"

    def apply(self, event: Event) -> None:
        """Двигает состояние одним событием. Не бросает наружу: см. `IngestRunner._emit`."""
        payload = event.payload
        self.last_event_at = event.ts

        match event.kind:
            case "run_started":
                self.roll_day(payload.get("day") or self.day)
                self.status = "running"
                self.run_id = payload.get("run_id")
                self.trigger = payload.get("trigger")
                self.started_at = event.ts
                self.in_flight.clear()
            case "counters":
                self.phase = payload.get("phase") or self.phase
                self.browse_offset = payload.get("browse_offset", self.browse_offset)
                self.claimed += payload.get("claimed", 0)
            case "game_started":
                self.in_flight[payload["slug"]] = payload.get("title") or payload["slug"]
            case "game_done":
                self.in_flight.pop(payload["slug"], None)
                self.ok += 1
            case "game_failed":
                self.in_flight.pop(payload["slug"], None)
                self.failed += 1
            case "llm_call" | "letsplay":
                # Заключение по летсплею — та же третья точка вызова модели
                # (design §5.1), и в дневной счётчик она входит вместе с
                # резюме: иначе после рестарта `restore` (он читает `runs`)
                # показал бы больше вызовов, чем накопила лента.
                self.llm_calls += payload.get("calls", 0)
                self.llm_failures += payload.get("failures", 0)
            case "run_finished":
                # Заход, не начавшийся из-за локи, приходит сюда с `run_id=None`
                # и не имеет права погасить статус того захода, который идёт.
                if payload.get("run_id") is not None and payload.get("run_id") != self.run_id:
                    return
                if payload.get("run_id") is None and self.status == "running":
                    return
                self.status = "idle"
                self.run_id = None
                self.in_flight.clear()


_state: WorkerState | None = None


def get_state() -> WorkerState:
    """Одно состояние на процесс: воркер пишет, веб читает."""
    global _state
    if _state is None:
        _state = WorkerState()
    return _state


def reset_state() -> None:
    """Сброс состояния между тестами."""
    global _state
    _state = None


async def restore(day: date | None = None) -> WorkerState:
    """Поднимает дневные агрегаты из БД при старте процесса.

    `status` всегда `idle`: строка `runs.status='running'`, оставшаяся от
    убитого процесса, означает не идущий заход, а брошенный — новый процесс
    ничего не обрабатывает, пока не сработает планировщик или кнопка.
    """
    day = day or today()
    state = get_state()
    state.day = day
    try:
        counters = await ProcessedRepo().counters(day)
        cursor = await DayCursorRepo().get(day)
        async with get_engine().connect() as conn:
            llm = (await conn.execute(_LLM_TOTALS, {"day": day})).one()
    except Exception:  # noqa: BLE001 — страница статуса не стоит падения старта
        log.exception("состояние воркера за %s не восстановлено, счётчики с нуля", day)
        return state

    state.status = "idle"
    state.run_id = None
    state.in_flight.clear()
    state.claimed, state.ok, state.failed = counters.claimed, counters.ok, counters.failed
    state.llm_calls, state.llm_failures = int(llm.calls), int(llm.failures)
    if cursor is not None:
        state.phase, state.browse_offset = cursor.phase, cursor.browse_offset
    log.info(
        "состояние за %s восстановлено: claimed=%s ok=%s failed=%s, фаза %s",
        day, state.claimed, state.ok, state.failed, state.phase,
    )
    return state
