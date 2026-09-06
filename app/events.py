"""Шина событий обхода (T-35, design §4.3, ADR-5).

Брокера нет и не будет: событий десятки в минуту, подписчиков — открытые
вкладки `/status`, всё живёт в одном процессе. Шина держит кольцевой буфер
последних событий (первая отрисовка страницы) и по очереди на подписчика.

Два свойства важнее остальных:

- `publish` синхронный и никогда не ждёт. Воркер публикует из середины обхода,
  и подписчик, который не вычитывает свою очередь (заснувшая вкладка, медленный
  канал), не имеет права затормозить обход даже на один `await`.
- Переполненная очередь теряет **старые** события, а не новые. Отставший клиент
  получит свежую картину со следующего события, а не ленту из позавчера; память
  при этом ограничена сверху `queue_size` на подписчика.

События — вещь эфемерная: буфер рестарт не переживает, а агрегаты страницы
статуса после рестарта восстанавливаются из БД (`app/state.py`).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Self

log = logging.getLogger(__name__)

EventKind = Literal[
    "run_started",
    "game_started",
    "game_done",
    "game_failed",
    "llm_call",
    "letsplay",
    "run_finished",
    "counters",
]

QUEUE_SIZE = 100    # событий на подписчика, дальше вытесняются старые
BUFFER_SIZE = 200   # кольцевой буфер шины (design §3.4)
RECENT_DEFAULT = 50


@dataclass(frozen=True)
class Event:
    """Событие обхода. `payload` — свободный словарь, его читают шаблоны статуса.

    Порядок полей отличается от design §4.3 (`ts` там первым): `ts`
    проставляется сам и вручную не передаётся, поэтому стоит после обязательных.
    """

    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


class Subscription:
    """Очередь одного подписчика; она же async-итератор из design §4.3.

    Отдельный класс, а не async-генератор, ровно из-за keep-alive в SSE (T-38):
    там ожидание события обёрнуто в `wait_for`, а отмена `__anext__` у
    генератора закрыла бы его насовсем. Отмена `Queue.get` безопасна, и
    подписка переживает любое число таймаутов.
    """

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._queue = queue

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    async def next(self, timeout: float) -> Event | None:
        """Следующее событие или `None`, если за `timeout` секунд ничего не было."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        self._bus.unsubscribe(self._queue)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """Реализация `EventBus` из design §4.3."""

    def __init__(self, *, queue_size: int = QUEUE_SIZE, buffer_size: int = BUFFER_SIZE) -> None:
        self._queue_size = queue_size
        self._recent: deque[Event] = deque(maxlen=buffer_size)
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def publish(self, event: Event) -> None:
        """Кладёт событие в буфер и в очередь каждому подписчику. Не блокирует."""
        self._recent.append(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # drop-oldest: место освобождает сам publish, а он единственный
                # писатель в очередь, так что второй put_nowait уже пройдёт.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — читатель успел вычитать
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover — очередь нулевого размера
                    log.warning("подписчик потерял событие %s", event.kind)

    def subscribe(self) -> Subscription:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return Subscription(self, queue)

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def recent(self, n: int = RECENT_DEFAULT) -> list[Event]:
        """Последние `n` событий в порядке публикации."""
        return list(self._recent)[-n:] if n > 0 else []

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    """Одна шина на процесс: у воркера и у SSE-эндпоинта она общая."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """Сброс шины между тестами."""
    global _bus
    _bus = None
