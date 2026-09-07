"""T-44: пайплайн летсплея — статусы в БД, одна попытка, отсутствие исключений.

Адаптеры подменены, БД настоящая: проверяется как раз то, что мок не
воспроизведёт — `ON CONFLICT (game_id)`, накопительный `attempts` и то, что
отказ внешнего сервиса оседает статусом, а не летит наружу. Игры берут id из
служебного диапазона, как в остальных тестах с БД.
"""

from __future__ import annotations

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import Product
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.ingest.upsert import upsert_game
from app.letsplay.dto import VideoCandidate
from app.letsplay.pipeline import LetsplayPipeline
from app.letsplay.retelling import RetellingUnavailable
from app.llm.schemas import LetsplayConclusionOut, LlmResult

GAME_ID = 9_000_700_001
SLUG = "zzq-letsplay-1"
TITLE = "ZZQ Letsplay"

VIDEO = VideoCandidate(
    video_id="vid1",
    video_url="https://www.youtube.com/watch?v=vid1",
    title="ZZQ Letsplay — полное прохождение",
    channel="ZZQ Plays",
    view_count=421_000,
    duration_s=4200,
)
RETELLING = "Блогер проходит игру и хвалит боевую систему."
CONCLUSION = "Бодрый экшен с внятной боевой системой и затянутым началом."


@pytest.fixture(autouse=True)
async def game_row():
    try:
        await _delete_test_rows()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    await upsert_game(Product(id=GAME_ID, slug=SLUG, title=TITLE, raw={"slug": SLUG}), {})
    try:
        yield
        await _delete_test_rows()
    finally:
        await dispose_engine()


async def _delete_test_rows() -> None:
    async with get_engine().begin() as conn:
        # letsplays уходит вместе с игрой: ON DELETE CASCADE
        await conn.execute(text("DELETE FROM games WHERE id >= 9000000000"))


async def row() -> dict | None:
    async with get_engine().connect() as conn:
        found = (
            await conn.execute(
                text("SELECT * FROM letsplays WHERE game_id = :id"), {"id": GAME_ID}
            )
        ).mappings().first()
    return dict(found) if found else None


class FakeFinder:
    def __init__(self, candidate: VideoCandidate | None = VIDEO) -> None:
        self.candidate = candidate
        self.calls: list[str] = []

    async def find(self, game_title: str) -> VideoCandidate | None:
        self.calls.append(game_title)
        return self.candidate


class FakeRetelling:
    """Пересказ без сети: либо текст, либо заранее заданный отказ сервиса."""

    def __init__(self, *, text: str = RETELLING, error: Exception | None = None,
                 enabled: bool = True) -> None:
        self.text = text
        self.error = error
        self._enabled = enabled
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def retell(self, video_url: str) -> str:
        self.calls.append(video_url)
        if self.error is not None:
            raise self.error
        return self.text

    async def aclose(self) -> None:
        return None


class FakeLlm:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    async def conclude_letsplay(self, *, game_title, retelling, context=None):
        self.calls.append({"title": game_title, "retelling": retelling, "context": context})
        if not self.ok:
            return LlmResult(ok=False, error="429: RESOURCE_EXHAUSTED",
                             prompt_version="letsplay_conclusion.v1", model="fake")
        return LlmResult(ok=True, value=LetsplayConclusionOut(conclusion=CONCLUSION),
                         prompt_version="letsplay_conclusion.v1", model="fake")


def pipeline(
    *,
    finder: FakeFinder | None = None,
    retelling: FakeRetelling | None = None,
    llm: FakeLlm | None = None,
    **settings_overrides,
) -> LetsplayPipeline:
    settings = get_settings().model_copy(update={"letsplay_enabled": True, "llm_enabled": True,
                                                 **settings_overrides})
    return LetsplayPipeline(
        settings,
        finder=finder or FakeFinder(),
        retelling=retelling or FakeRetelling(),
        llm=llm or FakeLlm(),
    )


async def test_full_chain_writes_ok_with_video_retelling_and_conclusion():
    result = await pipeline().enrich(GAME_ID, TITLE, run_id=7)

    assert (result.status, result.conclusion) == ("ok", CONCLUSION)
    assert (result.llm_calls, result.llm_failures) == (1, 0)
    saved = await row()
    assert saved is not None
    assert saved["status"] == "ok"
    assert saved["video_url"] == VIDEO.video_url
    assert saved["video_title"] == VIDEO.title
    assert (saved["channel"], saved["view_count"]) == ("ZZQ Plays", 421_000)
    assert saved["retelling"] == RETELLING
    assert saved["conclusion"] == CONCLUSION
    assert saved["attempts"] == 1 and saved["error"] is None
    assert saved["last_attempt_at"] is not None


async def test_retelling_goes_to_the_model_as_data_with_the_game_title():
    llm = FakeLlm()
    await pipeline(llm=llm).enrich(GAME_ID, TITLE, run_id=7)

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["title"] == TITLE and call["retelling"] == RETELLING
    assert call["context"] == {"game_id": GAME_ID, "run_id": 7, "kind": "letsplay"}


async def test_second_pass_over_a_done_game_costs_nothing():
    """Пересказ того же ролика не меняется — второй раз не платим (как quotes_hash)."""
    await pipeline().enrich(GAME_ID, TITLE)

    finder, retelling, llm = FakeFinder(), FakeRetelling(), FakeLlm()
    result = await pipeline(finder=finder, retelling=retelling, llm=llm).enrich(GAME_ID, TITLE)

    assert result.status == "ok"
    assert (finder.calls, retelling.calls, llm.calls) == ([], [], [])
    assert (await row())["attempts"] == 1


async def test_video_not_found_gives_not_found_without_calling_the_service():
    retelling = FakeRetelling()
    result = await pipeline(finder=FakeFinder(None), retelling=retelling).enrich(GAME_ID, TITLE)

    assert result.status == "not_found"
    saved = await row()
    assert saved["status"] == "not_found" and saved["video_url"] is None
    assert saved["attempts"] == 1
    assert retelling.calls == []


async def test_service_failure_keeps_the_video_and_counts_the_attempt():
    retelling = FakeRetelling(error=RetellingUnavailable("auth", "403 forbidden"))
    pipe = pipeline(retelling=retelling)

    first = await pipe.enrich(GAME_ID, TITLE)
    second = await pipe.enrich(GAME_ID, TITLE)

    assert (first.status, second.status) == ("service_error", "service_error")
    saved = await row()
    assert saved["status"] == "service_error"
    assert saved["video_url"] == VIDEO.video_url and saved["retelling"] is None
    assert "auth" in saved["error"]
    # неудача не «залипает»: следующий обход пробует снова, попытки копятся
    assert saved["attempts"] == 2


async def test_missing_session_gives_the_same_status_for_every_game():
    """Пустой YA300_SESSION_ID (DoD T-44): предсказуемый статус, без поиска роликов."""
    finder = FakeFinder()
    result = await pipeline(
        finder=finder, retelling=FakeRetelling(enabled=False)
    ).enrich(GAME_ID, TITLE)

    assert result.status == "service_error"
    assert "no_session" in (await row())["error"]
    assert finder.calls == []


async def test_llm_failure_is_a_service_error_with_the_retelling_kept():
    result = await pipeline(llm=FakeLlm(ok=False)).enrich(GAME_ID, TITLE)

    assert result.status == "service_error"
    assert (result.llm_calls, result.llm_failures) == (1, 1)
    saved = await row()
    assert saved["retelling"] == RETELLING and saved["conclusion"] is None
    # отказ модели уже пришёл с причиной — второго префикса поверх неё нет,
    # иначе `error_kind` на странице статуса показал бы `llm`, а не квоту
    assert saved["error"] == "429: RESOURCE_EXHAUSTED"


async def test_llm_switched_off_keeps_the_retelling_and_marks_disabled():
    llm = FakeLlm()
    result = await pipeline(llm=llm, llm_enabled=False).enrich(GAME_ID, TITLE)

    assert result.status == "disabled"
    assert llm.calls == []
    saved = await row()
    assert saved["status"] == "disabled" and saved["retelling"] == RETELLING


async def test_feature_switched_off_writes_nothing_at_all():
    finder = FakeFinder()
    result = await pipeline(finder=finder, letsplay_enabled=False).enrich(GAME_ID, TITLE)

    assert result.status == "disabled"
    assert finder.calls == []
    assert await row() is None


async def test_unexpected_exception_becomes_a_status_not_a_crash():
    """DoD T-44: вызывающая сторона не обязана оборачивать `enrich` в try/except."""

    class BrokenFinder:
        async def find(self, game_title: str):
            raise RuntimeError("yt-dlp упал внутри")

    result = await pipeline(finder=BrokenFinder()).enrich(GAME_ID, TITLE)

    assert result.status == "service_error"
    saved = await row()
    assert saved["status"] == "service_error" and "yt-dlp упал внутри" in saved["error"]
