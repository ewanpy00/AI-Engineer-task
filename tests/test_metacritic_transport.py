"""T-05: транспорт клиента Metacritic — rate-limit, retry, ошибки."""

from __future__ import annotations

import time

import httpx
import pytest

from app.clients import metacritic as mc
from app.config import Settings


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x@localhost/x",
        metacritic_base_url="https://backend.metacritic.com",
        metacritic_rps=1.0,
        metacritic_max_retries=3,
        metacritic_timeout_s=5.0,
    )
    base.update(overrides)
    return Settings(**base)


def transport_with(handler, **overrides) -> mc.MetacriticTransport:
    settings = make_settings(**overrides)
    client = httpx.AsyncClient(
        base_url=settings.metacritic_base_url,
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": settings.metacritic_user_agent},
    )
    return mc.MetacriticTransport(settings, client=client)


async def test_two_requests_are_spaced_by_at_least_one_second():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"ok": True}})

    t = transport_with(handler)
    started = time.monotonic()
    await t.get_json("/first")
    await t.get_json("/second")
    elapsed = time.monotonic() - started
    assert elapsed >= 1.0, f"два запроса разнесены только на {elapsed:.3f}s"
    await t.aclose()


async def test_user_agent_is_explicit_and_not_a_library_default():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={"data": {}})

    t = transport_with(handler, metacritic_rps=1000)
    await t.get_json("/x")
    assert seen[0] and not seen[0].startswith(("python-", "httpx/", "Java/"))
    await t.aclose()


async def test_500_is_retried_and_surfaces_as_metacritic_error(monkeypatch):
    monkeypatch.setattr(mc, "BACKOFF_BASE_S", 0.01)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="upstream boom")

    t = transport_with(handler, metacritic_rps=1000, metacritic_max_retries=3)
    with pytest.raises(mc.MetacriticError) as excinfo:
        await t.get_json("/games/metacritic/whatever/web")
    assert calls == 3, "должно быть ровно три попытки"
    assert excinfo.value.status == 500
    assert "upstream boom" in excinfo.value.body_excerpt
    assert "/games/metacritic/whatever/web" in excinfo.value.url
    await t.aclose()


async def test_500_then_200_returns_payload(monkeypatch):
    monkeypatch.setattr(mc, "BACKOFF_BASE_S", 0.01)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, json={"data": {"item": {"title": "X"}}})

    t = transport_with(handler, metacritic_rps=1000)
    payload = await t.get_json("/x")
    assert payload["data"]["item"]["title"] == "X"
    assert calls == 2
    await t.aclose()


async def test_429_respects_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(mc.asyncio, "sleep", fake_sleep)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": {}})

    t = transport_with(handler, metacritic_rps=1000)
    await t.get_json("/x")
    assert slept and 7.0 <= slept[0] < 7.3  # Retry-After + джиттер
    await t.aclose()


async def test_404_is_not_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="not found")

    t = transport_with(handler, metacritic_rps=1000)
    with pytest.raises(mc.MetacriticError) as excinfo:
        await t.get_json("/nope")
    assert calls == 1
    assert excinfo.value.status == 404
    await t.aclose()


async def test_timeout_never_leaks_raw_httpx_exception(monkeypatch):
    monkeypatch.setattr(mc, "BACKOFF_BASE_S", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    t = transport_with(handler, metacritic_rps=1000, metacritic_max_retries=2)
    with pytest.raises(mc.MetacriticError) as excinfo:
        await t.get_json("/slow")
    assert excinfo.value.status is None
    await t.aclose()


async def test_non_json_body_becomes_metacritic_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    t = transport_with(handler, metacritic_rps=1000)
    with pytest.raises(mc.MetacriticError):
        await t.get_json("/html")
    await t.aclose()
