"""T-24: единственный адаптер к LLM — Google AI Studio (Gemini).

По 11-decisions.md (OQ-1 + поправка к ADR-7) провайдер — Gemini, а не Anthropic
из design §5/ADR-7: структурированный вывод остаётся, но берётся через
`responseSchema`, а не через tool use. Контракты `LlmClient`/`LlmResult` из
design §4.4 сохранены как форма — меняется только реализация.

Три свойства, на которых держится весь LLM-слой:
  * наружу не летит ни одно исключение — отказ приходит как `LlmResult(ok=False)`,
    потому что резюме обогащает карточку и не должно ронять обход (design §5.5);
  * каждая попытка, включая неудачную, оседает строкой в JSONL (design §5.4);
  * у всех отказов один вид — `причина: текст`, см. `llm_error`. Причина —
    машинный токен (HTTP-код, `schema`, имя класса исключения), и по нему
    веб-слой решает, что показать на открытой всем странице статуса
    (`app.web.templating.error_kind`).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.clients.dto import Audience, Quote
from app.config import Settings, get_settings
from app.llm.jsonl_logger import JsonlLogger, get_jsonl_logger, utc_now_iso
from app.llm.prompts import Prompt, PromptRepo, get_prompt_repo
from app.llm.schemas import LetsplayConclusionOut, LlmResult, ReviewSummaryOut

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# design §5.5: 3 попытки, экспоненциальный backoff 2/4/8 с + джиттер.
# Джиттер множителем, а не слагаемым: так он масштабируется вместе с паузой
# и обнуляется вместе с ней, когда backoff отключён (тесты).
BACKOFF_BASE_S = 2.0
JITTER = 0.25
ERROR_LIMIT = 500
QUOTE_LIMIT = 2000  # верхняя граница на цитату (design §5.3), дубль страховки к T-30

PROMPT_BY_AUDIENCE = {
    "critic": "review_summary_critic",
    "user": "review_summary_user",
}
LETSPLAY_PROMPT = "letsplay_conclusion"

# Коды Gemini, на которых имеет смысл повторить (design §5.5; конкретные коды
# Anthropic из дизайна к Gemini неприменимы — здесь HTTP-статусы AI Studio).
RETRYABLE_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
# Нет ключа / нет доступа: ретрай бессмыслен, дальше молчим до конца захода.
FATAL_CODES = frozenset({401, 403})
# Схема или слишком длинный ввод: без ретрая, но одна попытка на урезанном вводе.
SHRINK_CODES = frozenset({400, 413, 422})


@dataclass
class _Attempt:
    """Исход одной попытки — то, из чего собирается и результат, и строка лога."""

    value: BaseModel | None = None
    error: str | None = None
    retryable: bool = False
    fatal: bool = False
    shrink: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_response: Any = None


def llm_error(reason: str | int, detail: str | None = None) -> str:
    """Единый вид отказа LLM: `причина: текст`.

    Причина — один машинный токен, дальше произвольный текст. Форма не
    косметическая: `error_kind` в веб-слое режет строку по первому двоеточию и
    наружу отдаёт только причину, поэтому «429 ClientError: 429
    RESOURCE_EXHAUSTED» на странице выглядело бы просто «ошибка», а квота —
    самая частая причина отказа на бесплатном тире (11-decisions.md, OQ-1).

    Повтор причины в тексте убирается: у ошибок google-genai сообщение само
    начинается с HTTP-кода, и без этого код попадал бы в строку дважды.
    """
    prefix = str(reason)
    text = (detail or "").strip()
    while prefix and text.startswith(prefix):
        text = text[len(prefix) :].lstrip(" :")
    return f"{prefix}: {text}"[:ERROR_LIMIT] if text else prefix


def escape_review_text(text: str) -> str:
    """Экранирует угловые скобки внутри цитаты (design §5.3).

    Отзыв, содержащий `</review>`, иначе закрыл бы контейнер и всё, что дальше,
    выглядело бы для модели как текст от нас, а не как данные пользователя.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_quotes(quotes: Sequence[Quote], *, limit: int = QUOTE_LIMIT) -> str:
    """Собирает `user`-сообщение с цитатами (design §5.3).

    НЕДОВЕРЕННЫЙ ВВОД: единственное место, где текст отзывов попадает в запрос,
    и попадает он только сюда — в отдельное сообщение роли `user`, обёрнутым в
    `<review>`. В system-промпт цитаты не подставляются никогда (CLAUDE.md).
    """
    blocks = [
        f'<review id="{n}" bucket="{q.bucket}">{escape_review_text(q.text[:limit])}</review>'
        for n, q in enumerate(quotes, start=1)
    ]
    return "\n".join(blocks)


class _CircuitBreaker:
    """design §5.5: пять подряд неудач — и LLM выключается до конца захода.

    Считает подряд идущие неудачи, а не общий процент: смысл в том, чтобы не
    жечь пятнадцать минут захода на таймаутах, когда провайдер лежит целиком.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._failures = 0
        self._open = False
        self.reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self, error: str, *, fatal: bool = False) -> None:
        self._failures += 1
        if fatal or self._failures >= self._limit:
            self._open = True
            self.reason = error
            log.warning("LLM отключён до конца захода: %s", error)

    def reset(self) -> None:
        """Вызывается в начале захода (T-32): breaker живёт один заход."""
        self._failures = 0
        self._open = False
        self.reason = None


class GeminiClient:
    """Реализация `LlmClient` из design §4.4 поверх google-genai."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        prompts: PromptRepo | None = None,
        logger: JsonlLogger | None = None,
        client: Any | None = None,
        backoff_base_s: float = BACKOFF_BASE_S,
    ) -> None:
        self._settings = settings or get_settings()
        self._prompts = prompts or get_prompt_repo()
        self._logger = logger or get_jsonl_logger()
        self._client = client
        self._backoff_base_s = backoff_base_s
        self._breaker = _CircuitBreaker(self._settings.gemini_failure_limit)
        self._slots = asyncio.Semaphore(max(1, self._settings.gemini_max_concurrency))

    # --- контракт design §4.4 ------------------------------------------------

    async def summarize_reviews(
        self,
        *,
        audience: Audience,
        game_title: str,
        quotes: Sequence[Quote],
        context: dict[str, Any] | None = None,
    ) -> LlmResult[ReviewSummaryOut]:
        prompt = self._prompts.load(PROMPT_BY_AUDIENCE[audience])
        return await self._call(
            prompt=prompt,
            schema=ReviewSummaryOut,
            user_text=self._reviews_user_text(game_title, quotes),
            shrink=lambda text: self._reviews_user_text(game_title, quotes[: max(1, len(quotes) // 2)]),
            context={"audience": audience, **(context or {})},
        )

    async def conclude_letsplay(
        self,
        *,
        game_title: str,
        retelling: str,
        context: dict[str, Any] | None = None,
    ) -> LlmResult[LetsplayConclusionOut]:
        """Доп. часть 1 (T-43). Пересказ 300.ya.ru — такой же недоверенный ввод."""
        prompt = self._prompts.load(LETSPLAY_PROMPT)
        # Название тоже приходит из Metacritic и экранируется наравне с
        # пересказом: иначе title вида `</retelling>…` закрыл бы контейнер и
        # всё, что за ним, модель прочитала бы как текст от нас.
        user_text = (
            f"Игра: {escape_review_text(game_title)}\n\n"
            f"<retelling>{escape_review_text(retelling)}</retelling>"
        )
        return await self._call(
            prompt=prompt,
            schema=LetsplayConclusionOut,
            user_text=user_text,
            shrink=lambda text: text[: len(text) // 2],
            context=context or {},
        )

    # --- внутреннее ----------------------------------------------------------

    @property
    def breaker(self) -> _CircuitBreaker:
        return self._breaker

    @property
    def model(self) -> str:
        return self._settings.gemini_model

    def reset_breaker(self) -> None:
        """Начало нового захода: прошлые неудачи больше не в счёт."""
        self._breaker.reset()

    @staticmethod
    def _reviews_user_text(game_title: str, quotes: Sequence[Quote]) -> str:
        # Название игры — в том же user-сообщении, что и цитаты: system-промпт
        # общий на все игры и от конкретной игры не зависит. Источник у названия
        # тот же, что у цитат (ответ Metacritic), поэтому и экранируется оно так
        # же: `</review>` в названии иначе разорвал бы разметку контейнеров.
        return f"Игра: {escape_review_text(game_title)}\n\n{render_quotes(quotes)}"

    def _genai_client(self) -> Any:
        if self._client is None:
            from google import genai  # локальный импорт: без ключа модуль не нужен

            self._client = genai.Client(api_key=self._settings.google_api_key)
        return self._client

    def _config(self, prompt: Prompt, schema: type[BaseModel]) -> Any:
        from google.genai import types

        # Замерено на живом API (gemini-3.8-flash, тот же промпт и цитаты):
        # без thinking_level вызов идёт ~43 с, с `low` — ~3 с, а на `high`
        # модель тратит ~860 токенов на размышления, упирается в
        # max_output_tokens и обрывает сам ответ. Для резюмирования готовых
        # цитат размышления не нужны, поэтому уровень задаётся в промпте.
        thinking = (
            types.ThinkingConfig(thinking_level=prompt.thinking_level)
            if prompt.thinking_level
            else None
        )
        return types.GenerateContentConfig(
            # system_instruction — единственное место, куда попадает текст
            # промпта; цитаты сюда не подставляются никогда (CLAUDE.md)
            system_instruction=prompt.text,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=prompt.temperature,
            max_output_tokens=prompt.max_tokens,
            thinking_config=thinking,
            # Инструментов у нас нет, схема приходит через responseSchema;
            # без явного отключения SDK пишет предупреждение об AFC на каждый вызов.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=int(self._settings.gemini_timeout_s * 1000)),
        )

    async def _call(
        self,
        *,
        prompt: Prompt,
        schema: type[T],
        user_text: str,
        shrink,
        context: dict[str, Any],
    ) -> LlmResult[T]:
        """Общий цикл: попытки, backoff, лог на каждую попытку, один результат."""
        base = {
            "prompt_version": prompt.version_tag,
            "prompt_sha256": prompt.sha256,
            "model": self.model,
            **context,
        }
        if self._breaker.is_open:
            error = llm_error("llm_disabled", self._breaker.reason)
            await self._log(base, attempt=0, messages=None, outcome=_Attempt(error=error))
            return self._failure(prompt, error, attempts=0)

        attempts = max(1, self._settings.gemini_max_retries)
        text = user_text
        error = "не выполнено ни одной попытки"
        attempt = 0
        shrunk = False

        while attempt < attempts:
            attempt += 1
            messages = self._messages(prompt, text)
            outcome = await self._attempt(prompt, schema, messages[1]["content"])
            await self._log(base, attempt=attempt, messages=messages, outcome=outcome)

            if outcome.value is not None:
                self._breaker.record_success()
                return LlmResult(
                    ok=True, value=outcome.value, prompt_version=prompt.version_tag,
                    prompt_sha256=prompt.sha256, model=self.model,
                    input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                    latency_ms=outcome.latency_ms, attempts=attempt,
                )

            error = outcome.error or "неизвестная ошибка"
            if outcome.fatal:
                self._breaker.record_failure(error, fatal=True)
                return self._failure(prompt, error, attempts=attempt)
            if outcome.shrink:
                # design §5.5: 400/422 не ретраятся «как есть» — режем ввод вдвое
                # и пробуем ровно один раз, дальше llm_failed.
                if shrunk:
                    break
                shrunk, text = True, shrink(text)
                log.info("%s: ввод урезан вдвое после %s", prompt.version_tag, error)
                continue
            if not outcome.retryable or attempt >= attempts:
                break
            await self._sleep_backoff(attempt)

        self._breaker.record_failure(error)
        return self._failure(prompt, error, attempts=attempt)

    async def _attempt(self, prompt: Prompt, schema: type[T], user_text: str) -> _Attempt:
        """Один сетевой вызов. Классифицирует любой сбой, ничего не пробрасывает."""
        started = time.monotonic()
        try:
            response = await self._generate(prompt, schema, user_text)
        except Exception as exc:  # noqa: BLE001 — классификация ниже, наружу не летит
            return self._classify(exc, int((time.monotonic() - started) * 1000))

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage_metadata", None)
        outcome = _Attempt(
            latency_ms=latency_ms,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )
        try:
            outcome.value = self._parse(response, schema)
            outcome.raw_response = outcome.value.model_dump()
        except (ValidationError, ValueError) as exc:
            # Ответ не лёг в схему: повторять с тем же вводом бессмысленно,
            # но урезанный ввод иногда помогает — идём по ветке shrink.
            outcome.error = llm_error("schema", str(exc))
            outcome.shrink = True
            outcome.raw_response = getattr(response, "text", None)
        return outcome

    async def _generate(self, prompt: Prompt, schema: type[BaseModel], user_text: str) -> Any:
        # Троттлинг обращений к модели — свой, не общий с Metacritic: у них
        # разные провайдеры и разные лимиты (см. `gemini_max_concurrency`).
        async with self._slots:
            return await self._genai_client().aio.models.generate_content(
                model=self.model,
                contents=user_text,
                config=self._config(prompt, schema),
            )

    @staticmethod
    def _parse(response: Any, schema: type[T]) -> T:
        """Разбор структурированного ответа — без ручного парсинга JSON.

        `responseSchema` уже заставил модель вернуть объект нужной формы, SDK
        отдаёт его в `parsed`. `model_validate_json` — путь на случай, когда
        SDK по какой-то причине оставил только текст.
        """
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("пустой ответ модели")
        return schema.model_validate_json(text)

    def _classify(self, exc: Exception, latency_ms: int) -> _Attempt:
        code = getattr(exc, "code", None)
        if not isinstance(code, int):
            code = getattr(getattr(exc, "response", None), "status_code", None)

        if isinstance(code, int):
            # Причина — сам код, а не класс исключения: у google-genai это на
            # все 4xx один и тот же `ClientError`, и различать отказы по нему
            # нельзя. Имя класса в тексте не нужно — сообщение и так с кодом.
            return _Attempt(
                error=llm_error(code, str(exc)),
                retryable=code in RETRYABLE_CODES,
                fatal=code in FATAL_CODES,
                shrink=code in SHRINK_CODES,
                latency_ms=latency_ms,
            )
        # Таймаут или обрыв соединения: кода нет, причина — класс исключения.
        retryable = isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)) or (
            type(exc).__module__.startswith("httpx")
        )
        return _Attempt(
            error=llm_error(type(exc).__name__, str(exc)),
            retryable=retryable,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _messages(prompt: Prompt, user_text: str) -> list[dict[str, str]]:
        """Ровно то, что уходит в модель, — и то, что попадёт в JSONL как есть.

        Порядок жёсткий: system — только текст промпта, user — только данные.
        По этой структуре в логе и проверяется правило «отзывы не инструкции».
        """
        return [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": user_text},
        ]

    def _failure(self, prompt: Prompt, error: str, *, attempts: int) -> LlmResult[Any]:
        return LlmResult(
            ok=False, value=None, error=error[:ERROR_LIMIT],
            prompt_version=prompt.version_tag, prompt_sha256=prompt.sha256,
            model=self.model, attempts=attempts,
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._backoff_base_s * (2 ** (attempt - 1)) * random.uniform(1.0, 1.0 + JITTER)
        log.info("повтор вызова LLM через %.1f с (попытка %s)", delay, attempt + 1)
        await asyncio.sleep(delay)

    async def _log(
        self,
        base: dict[str, Any],
        *,
        attempt: int,
        messages: list[dict[str, str]] | None,
        outcome: _Attempt,
    ) -> None:
        await self._logger.write(
            {
                "ts": utc_now_iso(),
                **base,
                "attempt": attempt,
                "status": "ok" if outcome.value is not None else "error",
                "latency_ms": outcome.latency_ms,
                "tokens": {"input": outcome.input_tokens, "output": outcome.output_tokens},
                "error": outcome.error,
                "messages": messages,
                "response": outcome.raw_response,
            }
        )


_client: GeminiClient | None = None


def get_llm_client() -> GeminiClient:
    """Один адаптер на процесс: circuit breaker и HTTP-пул общие."""
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client
