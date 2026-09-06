"""T-41: поиск летсплея через yt-dlp — выбор ролика и устойчивость к отказам.

Сеть подменена целиком: у finder'а одна точка выхода наружу (`ytdlp_search`),
её и заменяем. Проверяется то, ради чего задача делалась: из выдачи выбирается
самый просматриваемый *летсплей*, а не самый просматриваемый ролик, и любой
отказ YouTube (в т.ч. `LOGINREQUIRED` с датацентрового IP, ADR-8) превращается
в `None`, а не в исключение.
"""

from __future__ import annotations

from app.config import Settings
from app.letsplay.finder import YtDlpFinder, pick_most_viewed, to_candidate


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x@localhost/x",
        letsplay_search_results=10,
        letsplay_min_duration_s=300,
    )
    base.update(overrides)
    return Settings(**base)


def entry(video_id: str, *, views: int, duration: int | None = 3600, **extra) -> dict:
    return {
        "id": video_id,
        "title": f"Video {video_id}",
        "channel": "Some Channel",
        "view_count": views,
        "duration": duration,
        **extra,
    }


def finder_over(*entries, **overrides) -> tuple[YtDlpFinder, list[tuple]]:
    """Finder с зафиксированной выдачей; второй элемент — журнал вызовов поиска."""
    calls: list[tuple] = []

    def search(query: str, count: int, timeout_s: float):
        calls.append((query, count, timeout_s))
        return list(entries)

    return YtDlpFinder(make_settings(**overrides), search=search), calls


async def test_picks_the_most_viewed_long_video():
    found, calls = finder_over(
        entry("short", views=9_000_000, duration=95),      # трейлер: просмотров больше всех
        entry("play1", views=120_000, duration=2400),
        entry("play2", views=800_000, duration=5400),      # самый просматриваемый летсплей
    )

    candidate = await found.find("Some Game")

    assert candidate is not None
    assert candidate.video_id == "play2"
    assert candidate.video_url == "https://www.youtube.com/watch?v=play2"
    assert (candidate.view_count, candidate.duration_s) == (800_000, 5400)
    assert candidate.channel == "Some Channel"
    # запрос — в проверенной research §5.2 форме, счётчик результатов из конфига
    assert calls == [("Some Game let's play", 10, 30.0)]


async def test_falls_back_to_whole_output_when_nothing_is_long_enough():
    """Длительности может не быть в выдаче — это не повод отдать `not_found`."""
    found, _ = finder_over(
        entry("a", views=10, duration=None),
        entry("b", views=500, duration=None),
    )

    candidate = await found.find("Some Game")

    assert candidate is not None and candidate.video_id == "b"


async def test_empty_output_gives_none():
    found, _ = finder_over()
    assert await found.find("Some Game") is None


async def test_failing_search_gives_none_not_exception():
    """`LOGINREQUIRED` и прочие отказы YouTube — штатный сценарий (ADR-8)."""

    def search(query: str, count: int, timeout_s: float):
        raise RuntimeError("ERROR: [youtube] LOGINREQUIRED: Sign in to confirm")

    found = YtDlpFinder(make_settings(), search=search)
    assert await found.find("Some Game") is None


async def test_entries_without_id_are_skipped():
    """Без `id` ссылку не построить, а ролик без ссылки карточке не нужен."""
    found, _ = finder_over({"title": "no id at all", "view_count": 10_000_000})
    assert await found.find("Some Game") is None


def test_channel_falls_back_to_uploader():
    candidate = to_candidate({"id": "x", "title": "t", "uploader": "Uploader", "duration": 700})
    assert candidate is not None and candidate.channel == "Uploader"


def test_pick_prefers_views_over_duration():
    candidates = [
        to_candidate(entry("long", views=100, duration=20_000)),
        to_candidate(entry("popular", views=100_000, duration=1_200)),
    ]
    picked = pick_most_viewed([c for c in candidates if c], min_duration_s=300)
    assert picked is not None and picked.video_id == "popular"
