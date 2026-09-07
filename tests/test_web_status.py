"""T-38: страница `/status` и SSE-поток `/events`.

Браузер тут не запустишь, поэтому проверяется то, что отдаёт сервер: первичный
рендер из `WorkerState`, кадры `panel`/`feed` в формате SSE, keep-alive-пинг в
тишине и разметка кнопки ручного запуска. Поведение самого `htmx-ext-sse`
(переподключение при обрыве) — браузерное, его тест не покрывает.

Поток гоняется через генератор `stream()`, а не через HTTP-клиент: транспорт
httpx поверх ASGI дочитывает ответ до конца, прежде чем вернуть его, и на
бесконечном `text/event-stream` он просто зависает. Через HTTP проверяется то,
что от него зависит, — заголовки эндпоинта.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy.exc

from app.db import dispose_engine
from app.events import Event, get_bus, reset_bus
from app.main import app
from app.state import get_state, reset_state
from app.web import routes_status

TITLE = "ZZQ Status Game"


@pytest.fixture(autouse=True)
async def fresh_bus():
    reset_bus()
    reset_state()
    try:
        yield
    finally:
        reset_bus()
        reset_state()
        await dispose_engine()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def emit(kind: str, **payload: object) -> None:
    """То же, что `IngestRunner._emit`: событие двигает состояние и уходит в ленту."""
    event = Event(kind=kind, payload=payload)
    get_state().apply(event)
    get_bus().publish(event)


def running_state():
    """Заход в разгаре: одна игра в работе, батч уже выбран."""
    emit("run_started", run_id=42, trigger="manual")
    emit("counters", phase="browse", browse_offset=40, claimed=20)
    emit("game_started", slug="zzq", title=TITLE)
    return get_state()


async def get_status() -> httpx.Response:
    async with client() as http:
        try:
            return await http.get("/status")
        except (OSError, sqlalchemy.exc.OperationalError) as exc:
            pytest.skip(f"Postgres недоступен: {exc}")


async def test_status_page_renders_current_state():
    running_state()

    response = await get_status()

    assert response.status_code == 200
    assert TITLE in response.text          # текущая игра
    assert "идёт заход" in response.text    # статус воркера
    assert "#42" in response.text and "browse" in response.text


async def test_status_page_shows_events_from_the_buffer():
    """Клиент, открывший страницу посреди захода, видит уже случившееся."""
    emit("game_done", slug="zzq", title=TITLE)

    assert TITLE in (await get_status()).text


async def test_status_page_never_renders_the_admin_token():
    """Страница открыта всем: секрет в HTML сделал бы `/admin/run` анонимным.

    Кнопка остаётся на месте (её требует ТЗ), но токен подставляет тот, кто его
    знает, — заголовок собирается из поля ввода в момент запроса.
    """
    from app.config import get_settings

    text = (await get_status()).text
    token = get_settings().admin_token

    assert 'hx-post="/admin/run"' in text          # кнопка на месте
    assert 'id="admin-token"' in text              # токен вводится, а не рендерится
    assert "js:{" in text                          # заголовок вычисляется на клиенте
    assert token and token not in text


async def test_status_page_shows_the_effective_model():
    """«Какая модель у прода» должно быть видно из браузера.

    В резюме модель попадает только после успешной генерации, а когда её нет —
    именно этот вопрос и нужно задать первым.
    """
    from app.config import get_settings

    text = (await get_status()).text

    assert get_settings().gemini_model in text
    assert "ключ задан" in text  # факт, не значение


async def test_panel_frame_carries_the_model_too(monkeypatch):
    """SSE-кадр собирается тем же шаблоном — контекст у него должен совпадать."""
    from app.config import get_settings

    panel = routes_status.panel_frame(get_state())

    assert get_settings().gemini_model in panel


async def frames(stream, n: int) -> list[str]:
    """Первые `n` кадров потока; кадр — всё до пустой строки."""
    out: list[str] = []
    async for chunk in stream:
        out.append(chunk.removesuffix("\n\n"))
        if len(out) == n:
            break
    return out


async def test_events_endpoint_answers_with_an_event_stream():
    """Заголовки решают судьбу стрима: без них прокси и браузер его буферизуют."""
    response = await routes_status.events()

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    await response.body_iterator.aclose()


async def test_stream_opens_with_the_current_panel():
    """Клиент видит актуальные счётчики сразу, а не с первого события."""
    running_state()

    stream = routes_status.stream()
    try:
        [panel] = await frames(stream, 1)
    finally:
        await stream.aclose()

    assert panel.startswith("event: panel")
    assert TITLE in panel and "идёт заход" in panel


async def test_stream_pushes_every_new_event():
    running_state()
    stream = routes_status.stream()
    try:
        await frames(stream, 1)  # первичная панель

        emit("game_failed", slug="zzq", title=TITLE, error="MetacriticError: 500 http://db:5432")
        feed, panel = await frames(stream, 2)
    finally:
        await stream.aclose()

    assert feed.startswith("event: feed")
    # видно, что и на чём сломалось, но без текста исключения: страница открыта
    # всем, а в тексте — хосты, пути и параметры запросов
    assert TITLE in feed and "MetacriticError" in feed
    assert "db:5432" not in feed
    # панель приходит следом уже с новым счётчиком ошибок
    assert panel.startswith("event: panel") and "ошибок 1" in panel


async def test_stream_pings_when_nothing_happens(monkeypatch):
    """Идлящийся стрим прокси Railway рвёт — держим его комментарием (ADR-5)."""
    monkeypatch.setattr(routes_status, "KEEPALIVE_S", 0.05)

    stream = routes_status.stream()
    try:
        _, ping = await frames(stream, 2)
    finally:
        await stream.aclose()

    assert ping == ": ping"


async def test_closed_stream_releases_the_subscription():
    """Закрытая вкладка не должна оставлять за собой очередь на шине."""
    stream = routes_status.stream()
    await frames(stream, 1)
    assert get_bus().subscribers == 1

    await stream.aclose()

    assert get_bus().subscribers == 0


def test_multiline_html_survives_the_sse_frame_format():
    """Каждая строка HTML — своя `data:`; иначе браузер получит обрезанный кадр."""
    frame = routes_status.frame("feed", "<li>\n  первая\n  вторая\n</li>\n")

    assert frame == "event: feed\ndata: <li>\ndata:   первая\ndata:   вторая\ndata: </li>\n\n"


def test_event_frame_escapes_untrusted_text():
    """Ошибка из внешнего мира не имеет права стать разметкой."""
    event = Event(kind="game_failed",
                  payload={"slug": "zzq", "title": "<script>", "error": "<b>boom</b>"},
                  ts=datetime.now(UTC))

    frame = routes_status.event_frame(event)

    assert "<script>" not in frame and "&lt;script&gt;" in frame
    assert "<b>boom</b>" not in frame


def test_event_frame_hides_the_exception_text():
    """В ленту уходит машинная причина, а не текст исключения."""
    event = Event(
        kind="game_failed",
        payload={"slug": "zzq", "title": TITLE,
                 "error": "OperationalError: connect to user@10.0.0.5:5432 failed"},
        ts=datetime.now(UTC),
    )

    frame = routes_status.event_frame(event)

    assert "OperationalError" in frame
    assert "10.0.0.5" not in frame and "user@" not in frame
