"""T-44: пайплайн летсплея — find → retell → заключение LLM, статус в БД.

Три адаптера (T-41 поиск, T-42 пересказ, T-24 модель) собираются здесь в одну
best-effort операцию на игру. Правила, которые определяют весь модуль:

  * одна попытка за обход, без внутренних ретраев. Кука 300.ya.ru протухает
    целиком, а `LOGINREQUIRED` от YouTube с датацентрового IP не лечится
    повтором — повторы только жгут время захода (ADR-8, OQ-2);
  * наружу не летит ничего. Любой сбой — это статус в `letsplays`, а не
    исключение: игра уже сохранена по каталожным данным, и её `ok` в
    `processed_games` не должен зависеть от доступности неофициального
    сервиса (design §2.4, DoD T-44/T-45);
  * пересказ — недоверенный ввод (расшифровка чужой речи): в модель он уходит
    отдельным `user`-сообщением, обёрнутым в `<retelling>`, и никогда не
    попадает в system-промпт (CLAUDE.md, design §5.3).

Отличие от контракта design §4.5: `enrich` возвращает не `LetsplayStatus`, а
`LetsplayResult` — тот же статус плюс счётчики обращений к модели. Без них
заключение по летсплею не попало бы в `runs.llm_calls`, хотя это третья точка
вызова LLM в проекте (design §5.1).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text as sql

from app.config import Settings, get_settings
from app.db import get_engine
from app.letsplay.dto import LetsplayResult, VideoCandidate
from app.letsplay.finder import YtDlpFinder
from app.letsplay.retelling import RetellingUnavailable, Ya300RetellingService
from app.llm.gemini_client import GeminiClient, get_llm_client

log = logging.getLogger(__name__)

ERROR_LIMIT = 500

_UPSERT = sql(
    """
    INSERT INTO letsplays (
        game_id, status, video_id, video_url, video_title, channel, view_count,
        retelling, conclusion, attempts, error, last_attempt_at, updated_at
    ) VALUES (
        :game_id, :status, :video_id, :video_url, :video_title, :channel,
        :view_count, :retelling, :conclusion, 1, :error, now(), now()
    )
    ON CONFLICT (game_id) DO UPDATE SET
        status      = EXCLUDED.status,
        video_id    = EXCLUDED.video_id,
        video_url   = EXCLUDED.video_url,
        video_title = EXCLUDED.video_title,
        channel     = EXCLUDED.channel,
        view_count  = EXCLUDED.view_count,
        retelling   = EXCLUDED.retelling,
        conclusion  = EXCLUDED.conclusion,
        -- счётчик попыток накопительный: по нему видно игру, которую сервис
        -- отвергает каждый обход, а не только последний исход
        attempts    = letsplays.attempts + 1,
        error       = EXCLUDED.error,
        last_attempt_at = now(),
        updated_at  = now()
    """
)

_SELECT_STATUS = sql("SELECT status FROM letsplays WHERE game_id = :game_id")


class LetsplayPipeline:
    """Реализация `LetsplayPipeline` из design §4.5."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        finder: YtDlpFinder | None = None,
        retelling: Ya300RetellingService | None = None,
        llm: GeminiClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # Один семафор на обе сетевые операции: и поиск, и пересказ идут мимо
        # лимитера Metacritic, а параллелизм обхода (4) для неофициальных
        # сервисов слишком щедр.
        slots = asyncio.Semaphore(max(1, self._settings.letsplay_max_concurrency))
        self._finder = finder or YtDlpFinder(self._settings, slots=slots)
        self._retelling = retelling or Ya300RetellingService(self._settings)
        self._llm = llm
        self._slots = slots

    @property
    def llm(self) -> GeminiClient:
        """Лениво: обход с LETSPLAY_ENABLED=false ключа не требует."""
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm

    async def enrich(
        self, game_id: int, title: str, *, run_id: int | None = None
    ) -> LetsplayResult:
        """Один проход по игре. Исключений не бросает — исход в `LetsplayResult`."""
        if not self._settings.letsplay_enabled:
            # Фича выключена целиком: строку не пишем вовсе — попытки не было,
            # и помечать игру `disabled` значило бы соврать журналу.
            log.debug("летсплеи выключены (LETSPLAY_ENABLED=false): %s пропущен", title)
            return LetsplayResult(status="disabled")

        try:
            return await self._enrich(game_id, title, run_id)
        except Exception as exc:  # noqa: BLE001 — DoD T-44: наружу ничего не летит
            error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
            log.exception("летсплей для «%s» не собран", title)
            try:
                await self._save(game_id, status="service_error", error=error)
            except Exception:  # noqa: BLE001 — упала сама БД, писать некуда
                log.exception("не удалось сохранить отказ летсплея для game_id=%s", game_id)
            return LetsplayResult(status="service_error", error=error)

    async def _enrich(self, game_id: int, title: str, run_id: int | None) -> LetsplayResult:
        if await self._already_done(game_id):
            # Пересказ и заключение по одному и тому же ролику не меняются:
            # игра, обработанная в прошлые сутки, второй раз не оплачивается
            # (тот же принцип, что `quotes_hash` в резюме отзывов).
            log.debug("летсплей для «%s» уже собран, повторно не ходим", title)
            return LetsplayResult(status="ok")

        if not self._retelling.enabled:
            # Без куки пересказ не получить, а ролик без пересказа бесполезен —
            # поиск не начинаем вовсе. Статус предсказуемо один и тот же для
            # всех игр захода (DoD T-44), причина видна в `letsplays.error`.
            error = "no_session: YA300_SESSION_ID не задан"
            await self._save(game_id, status="service_error", error=error)
            return LetsplayResult(status="service_error", error=error)

        video = await self._finder.find(title)
        if video is None:
            await self._save(game_id, status="not_found")
            return LetsplayResult(status="not_found")

        try:
            async with self._slots:
                retelling = await self._retelling.retell(video.video_url)
        except RetellingUnavailable as exc:
            error = f"{exc.reason}: {exc.detail or ''}".strip(": ")[:ERROR_LIMIT]
            log.info("пересказ «%s» не получен: %s", video.video_url, error)
            await self._save(game_id, status="service_error", video=video, error=error)
            return LetsplayResult(status="service_error", video=video, error=error)

        if not self._settings.llm_enabled:
            # Пересказ есть, а заключения по нему ТЗ требует отдельно: без
            # модели фича не доделана, поэтому `disabled`, а не `ok`. Пересказ
            # всё равно сохраняем: по нему видно, что цепочка доехала до конца
            # и упёрлась именно в рубильник. Заключение допишет следующий
            # обход с включённой моделью — статус не `ok`, значит игра снова
            # попадёт в работу.
            error = "llm_disabled: LLM_ENABLED=false"
            await self._save(
                game_id, status="disabled", video=video, retelling=retelling, error=error
            )
            return LetsplayResult(status="disabled", video=video, error=error)

        result = await self.llm.conclude_letsplay(
            game_title=title,
            retelling=retelling,
            context={"game_id": game_id, "run_id": run_id, "kind": "letsplay"},
        )
        if not result.ok or result.value is None:
            error = f"llm: {result.error or 'нет ответа модели'}"[:ERROR_LIMIT]
            await self._save(
                game_id, status="service_error", video=video, retelling=retelling, error=error
            )
            return LetsplayResult(
                status="service_error", video=video, error=error,
                llm_calls=1, llm_failures=1,
            )

        conclusion = result.value.conclusion
        await self._save(
            game_id, status="ok", video=video, retelling=retelling, conclusion=conclusion
        )
        return LetsplayResult(
            status="ok", video=video, conclusion=conclusion, llm_calls=1
        )

    async def _already_done(self, game_id: int) -> bool:
        async with get_engine().connect() as conn:
            status = await conn.scalar(_SELECT_STATUS, {"game_id": game_id})
        return status == "ok"

    async def _save(
        self,
        game_id: int,
        *,
        status: str,
        video: VideoCandidate | None = None,
        retelling: str | None = None,
        conclusion: str | None = None,
        error: str | None = None,
    ) -> None:
        async with get_engine().begin() as conn:
            await conn.execute(
                _UPSERT,
                {
                    "game_id": game_id,
                    "status": status,
                    "video_id": video.video_id if video else None,
                    "video_url": video.video_url if video else None,
                    "video_title": video.title if video else None,
                    "channel": video.channel if video else None,
                    "view_count": video.view_count if video else None,
                    "retelling": retelling,
                    "conclusion": conclusion,
                    "error": error[:ERROR_LIMIT] if error else None,
                },
            )

    async def aclose(self) -> None:
        await self._retelling.aclose()


_pipeline: LetsplayPipeline | None = None


def get_letsplay_pipeline() -> LetsplayPipeline:
    """Один пайплайн на процесс: общий HTTP-клиент и общий потолок параллелизма.

    Закрывает его `IngestRunner.aclose` — единственный, кто им пользуется.
    """
    global _pipeline
    if _pipeline is None:
        _pipeline = LetsplayPipeline()
    return _pipeline

