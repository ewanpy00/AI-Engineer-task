"""T-42: адаптер 300.ya.ru — ожидание генерации, отказы, протухшая кука.

Транспорт подменён `httpx.MockTransport`: проверяется поведение адаптера, а не
живой сервис. Форма запросов и ответов здесь — не догадка: она снята с живого
API 07.09.2026 (см. docstring модуля). Первая версия адаптера получала 404 на
каждой игре ровно потому, что не отправляла обязательное поле `type`, поэтому
состав тела запроса тесты проверяют так же строго, как разбор ответа.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.letsplay.retelling import (
    RetellingUnavailable,
    Ya300RetellingService,
    extract_retelling,
)

VIDEO = "https://www.youtube.com/watch?v=abc123"

# Форма живого ответа: у видео заполнены keypoints с theses, а `title` и
# `sharing_url` остаются пустыми даже после завершения — признак готовности
# только `status_code == 0`.
DONE = {
    "status_code": 0,
    "session_id": "sess-1",
    "title": "",
    "sharing_url": "",
    "type": "video",
    "keypoints": [
        {"content": "Начало", "theses": [{"content": "Герой попадает в город"}]},
        {"content": "Бой", "theses": [{"content": "Драки быстрые"}, {"content": "Боссы жёсткие"}]},
    ],
}


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x@localhost/x",
        ya300_session_id="cookie-value",
        ya300_base_url="https://300.ya.ru",
        ya300_timeout_s=5.0,
    )
    base.update(overrides)
    return Settings(**base)


def service_over(*responses, **overrides) -> tuple[Ya300RetellingService, list[httpx.Request]]:
    """Сервис поверх очереди ответов; последний повторяется, если запросов больше."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses[min(len(seen) - 1, len(responses) - 1)]
        return outcome if isinstance(outcome, httpx.Response) else httpx.Response(200, json=outcome)

    settings = make_settings(**overrides)
    client = httpx.AsyncClient(
        base_url=settings.ya300_base_url,
        transport=httpx.MockTransport(handler),
        cookies={"Session_id": settings.ya300_session_id},
    )
    # poll_interval_s=0: ждать в тесте нечего, интервал проверяется отдельно
    return Ya300RetellingService(settings, client=client, poll_interval_s=0.0), seen


async def test_successful_call_returns_joined_retelling():
    service, seen = service_over(DONE)

    retelling = await service.retell(VIDEO)

    assert retelling.splitlines() == [
        "Начало", "Герой попадает в город", "Бой", "Драки быстрые", "Боссы жёсткие",
    ]
    assert len(seen) == 1
    assert seen[0].url.path == "/api/generation"
    assert "Session_id=cookie-value" in seen[0].headers.get("cookie", "")


async def test_start_request_carries_the_url_and_the_mandatory_type():
    """Без `type` сервис отвечает 404 — на этом не работала первая версия."""
    service, seen = service_over(DONE)

    await service.retell(VIDEO)

    assert json.loads(seen[0].content) == {"video_url": VIDEO, "type": "video"}


async def test_waits_until_generation_is_done():
    """`status_code=1` — «ещё генерируется»: продолжаем по `session_id`."""
    service, seen = service_over(
        {"status_code": 1, "session_id": "sess-1", "poll_interval_ms": 1},
        # промежуточный ответ: keypoints уже приходят, но частично — брать рано
        {"status_code": 1, "session_id": "sess-1", "poll_interval_ms": 1,
         "keypoints": [{"content": "Начало", "theses": []}]},
        DONE,
    )

    retelling = await service.retell(VIDEO)

    assert len(seen) == 3
    # в продолжении — session_id и type, ссылки на ролик там нет
    assert json.loads(seen[1].content) == {"session_id": "sess-1", "type": "video"}
    assert json.loads(seen[2].content) == {"session_id": "sess-1", "type": "video"}
    # текст взят из последнего ответа, а не из недособранного промежуточного
    assert "Боссы жёсткие" in retelling


async def test_generation_without_session_id_is_not_polled_forever():
    """Продолжать нечем: повтор по ссылке начал бы генерацию заново."""
    service, seen = service_over({"status_code": 1, "poll_interval_ms": 1})

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "rejected"
    assert len(seen) == 1


async def test_error_code_in_a_finished_answer_is_a_failure():
    service, _ = service_over(DONE | {"error_code": 3})

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "rejected"


async def test_expired_cookie_is_reported_as_auth_failure():
    """Живой сервис отвечает на протухшую куку `403 {"message":"Not Authorized"}`."""
    service, _ = service_over(httpx.Response(403, json={"message": "Not Authorized"}))

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "auth"


async def test_service_rejection_is_not_a_crash():
    service, _ = service_over({"status_code": 2, "message": "unsupported video"})

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "rejected"
    assert "unsupported video" in str(exc.value)


async def test_answer_without_text_is_not_saved_as_empty_retelling():
    service, _ = service_over({"status_code": 0, "keypoints": []})

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "empty"


async def test_missing_session_id_does_not_even_call_the_service():
    service, seen = service_over(DONE, ya300_session_id="")

    assert service.enabled is False
    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "no_session"
    assert seen == []


async def test_timeout_is_reported_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    settings = make_settings()
    client = httpx.AsyncClient(
        base_url=settings.ya300_base_url, transport=httpx.MockTransport(handler)
    )
    service = Ya300RetellingService(settings, client=client)

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "timeout"


async def test_non_json_answer_is_reported_as_unavailable():
    service, _ = service_over(httpx.Response(200, text="<html>captcha</html>"))

    with pytest.raises(RetellingUnavailable) as exc:
        await service.retell(VIDEO)
    assert exc.value.reason == "bad_json"


async def test_own_client_does_not_follow_redirects():
    """Кука `Session_id` не имеет права уехать на чужой хост.

    httpx создаёт куку из словаря с пустым доменом, а он матчится на любой
    хост: редирект с 300.ya.ru на сторонний домен унёс бы туда доступ к
    аккаунту Яндекса целиком.
    """
    service = Ya300RetellingService(make_settings())

    client = service._http()
    try:
        assert client.follow_redirects is False
    finally:
        await service.aclose()


def test_extract_reads_flat_thesis_list_too():
    """Форма ответа не верифицирована — разбор терпим к обеим её версиям."""
    assert extract_retelling({"thesis": [{"content": "раз"}, {"content": "два"}]}) == "раз\nдва"
    assert extract_retelling({"summary": "одним полем"}) == "одним полем"
    assert extract_retelling({"status_code": 0}) == ""
