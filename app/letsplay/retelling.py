"""T-42: пересказ ролика через 300.ya.ru.

Почему именно этот путь: транскрипт чужого видео официальный YouTube API не
отдаёт ни при каких условиях (нужен OAuth владельца канала), а неофициальные
пути блокируются именно на IP дата-центров — то есть там, где живёт сервис
(research §5, ADR-8). 300.ya.ru принимает ссылку и отдаёт готовый пересказ,
это единственный проверенный владельцем вручную вариант (11-decisions.md).

Контракт сервиса (проверен вживую 07.09.2026, первая реализация была написана
по догадке и получала 404 на каждой игре). Публичной документации у 300.ya.ru
нет; форма запроса взята из открытой реализации `gniloyprolaps/ya300`
(`prlps_ya300/utils.py`) и подтверждена запросами с реальной кукой:

  * `POST https://300.ya.ru/api/generation`, тело JSON;
  * запуск генерации — `{"video_url": …, "type": "video"}`. **Поле `type`
    обязательно**: без него сервис отвечает `404 {"message":"Not Found"}` при
    любых заголовках — ровно на этом и не работала первая версия;
  * продолжение — `{"session_id": …, "type": "video"}`, где `session_id` из
    первого ответа; ссылку на ролик повторять не нужно;
  * авторизация — кука `Session_id`; её одной достаточно (companion-куку
    `yandex_csyr` из референсной реализации сервис не требует, проверено).
    Протухшая кука или её отсутствие — `403 {"message":"Not Authorized"}`;
  * `status_code`: 1 — генерация идёт, 0 — готово, больше — отказ (тогда же
    приходит `error_code`). Пауза между опросами — `poll_interval_ms`,
    наблюдалось 500 мс, генерация двухчасового ролика занимала ~5 секунд;
  * текст лежит в `keypoints[].content` и `keypoints[].theses[].content`.
    Признак готовности — именно `status_code == 0`, а не `sharing_url`: у
    видео он оставался пустым и после завершения (референсная реализация ждёт
    его и потому на видео зависает). При `status_code == 1` keypoints тоже
    приходят, но частично — брать их рано.

Кеша у сервиса нет: повторный запрос того же ролика начинает генерацию заново
(`summary_age_seconds = 0`), поэтому повторов не делаем и на стороне пайплайна.

Контракт `RetellingService` из design §4.5 (`retell(video_url) -> str`,
`RetellingUnavailable` при любой беде) при этом не изменился: весь остальной
код видит только его (design §9, «формат API 300.ya.ru изолирован
интерфейсом»), и правка целиком уместилась в этот модуль.

Свойства, на которые опирается пайплайн (T-44):
  * одна попытка, без внутренних ретраев — кука протухает целиком, а не
    через раз, и повторы её не оживят (OQ-2, «дефолт принят»);
  * общий дедлайн `YA300_TIMEOUT_S` на весь вызов, включая ожидание
    генерации: пересказ не имеет права задержать обход дольше, чем на минуту;
  * наружу — только `RetellingUnavailable`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

GENERATION_PATH = "/api/generation"
SESSION_COOKIE = "Session_id"
# Обязательное поле тела: без него — 404. Пересказываем только ролики,
# статьи (`"article"`) сервису тоже доступны, но нам не нужны.
CONTENT_TYPE = "video"

STATUS_DONE = 0
STATUS_IN_PROGRESS = 1

POLL_INTERVAL_S = 0.5      # значение по умолчанию; сервис присылает своё
POLL_INTERVAL_MAX_S = 10.0
BODY_EXCERPT_LIMIT = 300
# Пересказ уходит в модель целиком, а его длину сервис не гарантирует.
# Верхняя граница — здесь, чтобы 400 от Gemini не приходил вместо пересказа.
RETELLING_LIMIT = 20_000

# Не-браузерный UA сервису неинтересен: он рассчитан на веб-версию, и
# дефолтный `python-httpx` — лишний повод получить отказ (тот же приём, что в
# клиенте Metacritic).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class RetellingUnavailable(Exception):
    """Пересказ получить не удалось. `reason` — короткая машинная причина.

    Отдельный тип, а не голый `httpx.HTTPError`, ровно из-за требования
    design §4.5: пайплайн должен отличать «сервис не смог» (статус
    `service_error` в БД) от собственных ошибок кода, которые чинятся, а не
    записываются в таблицу.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"300.ya.ru: {reason}" + (f" ({detail})" if detail else ""))


def _excerpt(response: httpx.Response) -> str:
    try:
        return response.text[:BODY_EXCERPT_LIMIT]
    except Exception:  # noqa: BLE001 — тело может быть недекодируемым
        return f"<{len(response.content)} bytes>"


def extract_retelling(payload: dict[str, Any]) -> str:
    """Собирает текст пересказа из ответа сервиса.

    У видео это `keypoints` — смысловые блоки с `content` и списком `theses`,
    у каждого тезиса тоже `content` (проверено на живых ответах). Плоский
    `thesis` для видео приходит пустым, но у статей он основной, поэтому
    разбираются обе формы. Неизвестная форма даёт пустую строку — вызывающий
    превратит её в `RetellingUnavailable`, а не в пустой пересказ в БД.
    """
    parts: list[str] = []
    for keypoint in payload.get("keypoints") or []:
        if not isinstance(keypoint, dict):
            continue
        title = str(keypoint.get("content") or "").strip()
        if title:
            parts.append(title)
        parts.extend(_thesis_texts(keypoint.get("theses")))
    parts.extend(_thesis_texts(payload.get("thesis")))
    if not parts:
        # Совсем другая форма ответа: даём тексту последний шанс, если сервис
        # положил пересказ одним полем.
        for key in ("summary", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
    return "\n".join(dict.fromkeys(parts))[:RETELLING_LIMIT]


def _thesis_texts(theses: Any) -> list[str]:
    if not isinstance(theses, list):
        return []
    out: list[str] = []
    for thesis in theses:
        if isinstance(thesis, dict):
            text = str(thesis.get("content") or thesis.get("text") or "").strip()
        else:
            text = str(thesis or "").strip()
        if text:
            out.append(text)
    return out


class Ya300RetellingService:
    """Реализация `RetellingService` из design §4.5."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client
        self._poll_interval_s = poll_interval_s

    @property
    def enabled(self) -> bool:
        """Без куки сервис не работает вовсе — пайплайн ставит `disabled`."""
        return bool(self._settings.ya300_session_id)

    async def retell(self, video_url: str) -> str:
        """Текст пересказа или `RetellingUnavailable`. Одна попытка."""
        if not self.enabled:
            raise RetellingUnavailable("no_session", "YA300_SESSION_ID не задан")

        deadline = time.monotonic() + self._settings.ya300_timeout_s
        data = await self._post({"video_url": video_url, "type": CONTENT_TYPE}, deadline)
        session = data.get("session_id")

        while data.get("status_code") == STATUS_IN_PROGRESS:
            if not session:
                # Без `session_id` продолжать нечем, а повторный запуск по
                # ссылке начал бы генерацию заново и зациклил нас.
                raise RetellingUnavailable("rejected", "сервис не выдал session_id")
            await self._wait(data, deadline)
            # Ссылка на ролик в продолжении не нужна и не передаётся: сервис
            # держит её за `session_id`.
            data = await self._post({"session_id": session, "type": CONTENT_TYPE}, deadline)

        status, error_code = data.get("status_code"), data.get("error_code")
        if status != STATUS_DONE or error_code:
            raise RetellingUnavailable(
                "rejected",
                f"status_code={status} error_code={error_code} "
                f"{str(data.get('message') or '')[:120]}".strip(),
            )

        retelling = extract_retelling(data)
        if not retelling:
            raise RetellingUnavailable("empty", "в ответе нет текста пересказа")
        return retelling

    async def _post(self, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
        left = deadline - time.monotonic()
        if left <= 0:
            raise RetellingUnavailable("timeout", "истёк общий дедлайн вызова")
        try:
            response = await self._http().post(GENERATION_PATH, json=payload, timeout=left)
        except httpx.TimeoutException as exc:
            raise RetellingUnavailable("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise RetellingUnavailable("transport", f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            # Самый ожидаемый отказ: сессионная кука истекла (ADR-8). Отличаем
            # его в логе от прочих, чтобы по логу было понятно, что чинить.
            raise RetellingUnavailable("auth", f"{response.status_code} {_excerpt(response)}")
        if response.status_code >= 400:
            raise RetellingUnavailable("http", f"{response.status_code} {_excerpt(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RetellingUnavailable("bad_json", _excerpt(response)) from exc
        if not isinstance(data, dict):
            raise RetellingUnavailable("bad_json", f"ожидался объект, пришёл {type(data).__name__}")
        return data

    async def _wait(self, data: dict[str, Any], deadline: float) -> None:
        interval = self._poll_interval_s
        raw = data.get("poll_interval_ms")
        if isinstance(raw, (int, float)) and raw > 0:
            interval = min(float(raw) / 1000.0, POLL_INTERVAL_MAX_S)
        left = deadline - time.monotonic()
        if left <= 0:
            raise RetellingUnavailable("timeout", "генерация не завершилась в срок")
        await asyncio.sleep(min(interval, left))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.ya300_base_url,
                timeout=self._settings.ya300_timeout_s,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                cookies={SESSION_COOKIE: self._settings.ya300_session_id},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
