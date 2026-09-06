"""Страница мониторинга и SSE-поток (T-38, ADR-5).

Транспорт — SSE, а не polling: поток односторонний, браузер переподключается
сам, а htmx-расширение подменяет фрагменты без единой строки собственного JS.

Сервер шлёт не JSON, а готовый HTML двумя именованными событиями:

- `panel` — блок счётчиков целиком, `sse-swap` заменяет его содержимое;
- `feed`  — одна строка ленты, она добавляется в начало списка.

Так на клиенте не остаётся ни рендеринга, ни состояния: единственный источник
разметки — те же Jinja-шаблоны, из которых собран первичный рендер страницы.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.events import RECENT_DEFAULT, Event, get_bus
from app.state import WorkerState, get_state
from app.web.repo_status import recent_runs
from app.web.templating import templates

log = logging.getLogger(__name__)

router = APIRouter()

# Идлящийся стрим прокси Railway рвёт; комментарий раз в 15 с держит соединение
# и заодно первым замечает отвалившегося клиента (ADR-5).
KEEPALIVE_S = 15.0

PANEL_TEMPLATE = "_status_panel.html"
EVENT_TEMPLATE = "_status_event.html"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # nginx-подобные прокси иначе копят ответ в буфере
}


def render(template: str, **context: object) -> str:
    """Фрагмент вне HTTP-ответа: Request'а у SSE-кадра нет, layout не нужен."""
    return templates.env.get_template(template).render(**context)


def frame(name: str, html: str) -> str:
    """Один SSE-кадр. Перевод строки в HTML — это `data:` следующей строкой."""
    lines = html.strip().splitlines() or [""]
    body = "\n".join(f"data: {line}" for line in lines)
    return f"event: {name}\n{body}\n\n"


def panel_frame(state: WorkerState) -> str:
    return frame("panel", render(PANEL_TEMPLATE, state=state))


def event_frame(event: Event) -> str:
    return frame("feed", render(EVENT_TEMPLATE, event=event))


@router.get("/status")
async def status_page(request: Request):
    """Первичный рендер: состояние воркера, лента из буфера шины, журнал заходов.

    Токен админки подставляется сервером прямо в разметку кнопки (OQ-8,
    «дефолт принят»): отдельного хранилища и отдельного логина у страницы нет,
    а без токена кнопка была бы неработающей декорацией.
    """
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "state": get_state(),
            "events": list(reversed(get_bus().recent(RECENT_DEFAULT))),  # свежие сверху
            "runs": await recent_runs(),
            "admin_token": get_settings().admin_token,
        },
    )


async def stream() -> AsyncIterator[str]:
    """Поток кадров для одного клиента.

    Подписка закрывается в `finally` — сюда приходит и обрыв соединения:
    Starlette закрывает генератор, когда клиент ушёл, иначе очередь копилась бы
    на каждую закрытую вкладку.
    """
    state = get_state()
    subscription = get_bus().subscribe()
    try:
        yield panel_frame(state)  # клиент видит актуальные счётчики сразу, а не с первым событием
        while True:
            event = await subscription.next(KEEPALIVE_S)
            if event is None:
                yield ": ping\n\n"
                continue
            yield event_frame(event)
            # Панель перерисовывается после каждого события: состояние уже
            # сдвинуто (`WorkerState.apply` вызван при публикации), и считать
            # счётчики на клиенте не нужно.
            yield panel_frame(state)
    finally:
        subscription.close()


@router.get("/events")
async def events() -> StreamingResponse:
    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
