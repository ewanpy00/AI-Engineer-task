"""T-30: пайплайн резюме отзывов — сборка цитат, вызов модели, запись, отказы.

Соединяет клиент Metacritic (T-27), промпты (T-25), адаптер Gemini (T-24) и
JSONL-лог (T-29) в одну функцию на игру. Три правила определяют весь модуль:

  * отзывы — недоверенный ввод: они уходят отдельным `user`-сообщением и
    никогда не попадают в system-промпт (CLAUDE.md, design §5.3);
  * `quotes_hash` решает, звать ли модель вообще: тот же набор цитат второй
    раз не оплачивается (design §3.2, §5.6);
  * отказ LLM не роняет обход каталога — он оседает статусом в
    `review_summaries` и дозаполнится следующим заходом (design §5.5).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text as sql

from app.clients.dto import Audience, Quote, ReviewQuotes
from app.clients.metacritic import MetacriticClient
from app.config import get_settings
from app.db import get_engine
from app.llm.gemini_client import GeminiClient, get_llm_client
from app.llm.schemas import ReviewSummaryOut

log = logging.getLogger(__name__)

AUDIENCES: tuple[Audience, ...] = ("critic", "user")

# design §5.3: верхние границы того, что вообще отдают summary-эндпоинты.
# Заодно фиксируют стоимость вызова.
MAX_QUOTES = {"critic": 14, "user": 20}
QUOTE_CHARS = 2000

# Размер страницы fallback-списка (T-30). У критиков сервер всё равно отдаёт 10.
FALLBACK_LIMIT = {"critic": 10, "user": 50}

# Порядок разбора подборки: сначала полярные мнения, потом нейтральные.
# Если брать подряд, при обрезке до 14 цитат в выборку попадёт один positive
# и модели не из чего собрать `disliked`.
BUCKET_ORDER = ("positive", "negative", "neutral", "default")

ERROR_LIMIT = 500

_UPSERT_SUMMARY = sql(
    """
    INSERT INTO review_summaries (
        game_id, audience, platform_slug, liked, disliked, tldr,
        quotes_count, quotes_hash, source, status, prompt_version, model, error,
        generated_at
    ) VALUES (
        :game_id, :audience, :platform_slug, CAST(:liked AS jsonb),
        CAST(:disliked AS jsonb), :tldr, :quotes_count, :quotes_hash, :source,
        :status, :prompt_version, :model, :error, now()
    )
    ON CONFLICT (game_id, audience) DO UPDATE SET
        platform_slug  = EXCLUDED.platform_slug,
        liked          = EXCLUDED.liked,
        disliked       = EXCLUDED.disliked,
        tldr           = EXCLUDED.tldr,
        quotes_count   = EXCLUDED.quotes_count,
        quotes_hash    = EXCLUDED.quotes_hash,
        source         = EXCLUDED.source,
        status         = EXCLUDED.status,
        prompt_version = EXCLUDED.prompt_version,
        model          = EXCLUDED.model,
        error          = EXCLUDED.error,
        generated_at   = now()
    """
)

_SELECT_EXISTING = sql(
    "SELECT quotes_hash, status FROM review_summaries "
    "WHERE game_id = :game_id AND audience = :audience"
)


@dataclass(frozen=True)
class Collected:
    """Цитаты, отобранные для одного вызова модели."""

    quotes: list[Quote]
    source: str | None  # summary_endpoint | review_list | None, если цитат нет

    @property
    def is_empty(self) -> bool:
        return not self.quotes


def quotes_hash(quotes: Sequence[Quote]) -> str:
    """sha256 нормализованного набора цитат (design §3.2).

    Набор, а не список: порядок в ответе Metacritic не гарантирован, и его
    перестановка не должна выглядеть как новые отзывы. Нормализуем пробелы —
    иначе переформатирование той же цитаты оплачивалось бы как новая.
    """
    normalized = sorted(f"{q.bucket}:{' '.join(q.text.split())}" for q in quotes)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def select_quotes(available: ReviewQuotes, audience: Audience) -> list[Quote]:
    """Обрезает подборку до лимита аудитории, чередуя тональности.

    Обрезка по длине (2000 символов) — тоже здесь: в `quotes_hash` и в лог
    должно попасть ровно то, что уйдёт в модель, а не исходный текст.
    """
    buckets = [list(getattr(available, name)) for name in BUCKET_ORDER]
    limit = MAX_QUOTES[audience]
    picked: list[Quote] = []
    while len(picked) < limit and any(buckets):
        for bucket in buckets:
            if not bucket:
                continue
            quote = bucket.pop(0)
            picked.append(_trim(quote))
            if len(picked) >= limit:
                break
    return picked


def _trim(quote: Quote) -> Quote:
    text = " ".join(quote.text.split())[:QUOTE_CHARS]
    return quote if text == quote.text else Quote(
        text=text, score=quote.score, author=quote.author, bucket=quote.bucket
    )


async def collect_quotes(
    client: MetacriticClient, slug: str, platform_slug: str, audience: Audience
) -> Collected:
    """Подборка по тональности, при пустом результате — fallback на список.

    Fallback — регулярный путь, а не исключение: 11-decisions.md, поправка 3 по
    живым данным (пустая подборка критиков при 92 отзывах).
    """
    summary = await client.get_review_summary(slug, platform_slug, audience)
    if not summary.is_empty():
        picked = select_quotes(summary, audience)
        if picked:
            return Collected(quotes=picked, source="summary_endpoint")

    reviews = await client.list_reviews(
        slug, platform_slug, audience, limit=FALLBACK_LIMIT[audience]
    )
    # Список отзывов тональностью не размечен — весь уходит в bucket `default`.
    fallback = ReviewQuotes(default=[r.to_quote() for r in reviews])
    picked = select_quotes(fallback, audience)
    return Collected(quotes=picked, source="review_list" if picked else None)


async def summarize_game_reviews(
    game_id: int,
    slug: str,
    lead_platform_slug: str | None,
    title: str,
    *,
    client: MetacriticClient | None = None,
    llm: GeminiClient | None = None,
    run_id: int | None = None,
) -> None:
    """Резюме отзывов одной игры: две аудитории, две строки в `review_summaries`.

    Наружу не бросает ничего: вызывается из `process_game` (T-32), где игра уже
    сохранена по каталожным данным и не должна получить `failed` из-за LLM.
    """
    if not get_settings().llm_enabled:
        # Рубильник для прогонов каталога без LLM: строк не пишем вовсе, чтобы
        # не пометить игру `llm_failed` там, где вызова просто не было.
        log.debug("LLM выключен (LLM_ENABLED=false): резюме %s пропущено", slug)
        return

    if lead_platform_slug is None:
        # Отзывы берутся только по ведущей платформе (11-decisions.md). Без неё
        # непонятно, чьи отзывы читать, — строку не пишем вовсе.
        log.info("игра %s без ведущей платформы: резюме отзывов пропущено", slug)
        return

    client = client or MetacriticClient()
    llm = llm or get_llm_client()
    for audience in AUDIENCES:
        try:
            await _summarize_audience(
                game_id, slug, lead_platform_slug, title, audience,
                client=client, llm=llm, run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 — одна аудитория не роняет вторую
            error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
            log.warning("резюме %s/%s не собрано: %s", slug, audience, error)
            await _save_failure(game_id, audience, lead_platform_slug, error)


async def _summarize_audience(
    game_id: int,
    slug: str,
    platform_slug: str,
    title: str,
    audience: Audience,
    *,
    client: MetacriticClient,
    llm: GeminiClient,
    run_id: int | None,
) -> None:
    collected = await collect_quotes(client, slug, platform_slug, audience)
    if collected.is_empty:
        # Ни подборки, ни списка — модель звать не на чем (design §5.5).
        await _save(
            game_id=game_id, audience=audience, platform_slug=platform_slug,
            status="no_data", source=None, quotes_count=0, quotes_hash=None,
        )
        return

    digest = quotes_hash(collected.quotes)
    if await _already_summarized(game_id, audience, digest):
        log.debug("резюме %s/%s актуально: набор цитат не менялся", slug, audience)
        return

    result = await llm.summarize_reviews(
        audience=audience,
        game_title=title,
        quotes=collected.quotes,
        context={"game_id": game_id, "run_id": run_id, "slug": slug},
    )
    if not result.ok or result.value is None:
        await _save(
            game_id=game_id, audience=audience, platform_slug=platform_slug,
            status="llm_failed", source=collected.source,
            quotes_count=len(collected.quotes),
            # хеш не сохраняем: иначе следующий заход счёл бы неудачу
            # актуальным резюме и не повторил бы вызов
            quotes_hash=None,
            prompt_version=result.prompt_version, model=result.model,
            error=result.error,
        )
        return

    summary: ReviewSummaryOut = result.value
    await _save(
        game_id=game_id, audience=audience, platform_slug=platform_slug,
        status="ok", source=collected.source, quotes_count=len(collected.quotes),
        quotes_hash=digest, summary=summary,
        prompt_version=result.prompt_version, model=result.model,
    )


async def _already_summarized(game_id: int, audience: Audience, digest: str) -> bool:
    """Тот же набор цитат уже отработан успешно — второй раз не платим.

    Сравнение только со статусом `ok`: строка `llm_failed` с тем же хешем —
    это невыполненная работа, её следующий заход обязан повторить (design §5.5,
    «следующий заход её дозаполнит»).
    """
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(_SELECT_EXISTING, {"game_id": game_id, "audience": audience})
        ).mappings().first()
    return bool(row and row["status"] == "ok" and row["quotes_hash"] == digest)


async def _save(
    *,
    game_id: int,
    audience: Audience,
    platform_slug: str,
    status: str,
    source: str | None,
    quotes_count: int,
    quotes_hash: str | None,
    summary: ReviewSummaryOut | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            _UPSERT_SUMMARY,
            {
                "game_id": game_id,
                "audience": audience,
                "platform_slug": platform_slug,
                "liked": json.dumps(summary.liked if summary else [], ensure_ascii=False),
                "disliked": json.dumps(summary.disliked if summary else [], ensure_ascii=False),
                "tldr": summary.tldr if summary else None,
                "quotes_count": quotes_count,
                "quotes_hash": quotes_hash,
                "source": source,
                "status": status,
                "prompt_version": prompt_version,
                "model": model,
                "error": error[:ERROR_LIMIT] if error else None,
            },
        )


async def _save_failure(
    game_id: int, audience: Audience, platform_slug: str, error: str
) -> None:
    """Сбой на нашей стороне (сеть Metacritic, БД) — тоже `llm_failed`.

    Отдельного статуса под это в схеме нет, а смысл для следующего захода тот
    же: работа не сделана, повторить.
    """
    try:
        await _save(
            game_id=game_id, audience=audience, platform_slug=platform_slug,
            status="llm_failed", source=None, quotes_count=0, quotes_hash=None,
            error=error,
        )
    except Exception:  # noqa: BLE001 — БД уже недоступна, писать некуда
        log.exception("не удалось сохранить отказ резюме для game_id=%s", game_id)
