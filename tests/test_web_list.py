"""T-14/T-15: витрина — карточка, поиск, фильтр, сортировка, пагинация.

Как и тесты upsert, идут в реальный Postgres из DATABASE_URL: проверяется
ровно то, чего мок не покажет — NULLS LAST, отсутствие дублей при фильтре по
платформе и экранирование символов LIKE. Игры синтетические, id из служебного
диапазона, чтобы не задеть реальные данные в базе.
"""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.clients.dto import PlatformInfo, Product, ScoreStats
from app.db import dispose_engine, get_engine
from app.ingest.upsert import upsert_game
from app.main import app
from app.web import repo_games

BASE_ID = 9_000_100_000
PLAT_A = "test-plat-a"
PLAT_B = "test-plat-b"
MARK = "Zzqtest"  # маркер в названии: по нему выбираются только свои игры


def product(n: int, title: str, platforms: list[PlatformInfo]) -> Product:
    return Product(
        id=BASE_ID + n,
        slug=f"zzqtest-{n}",
        title=title,
        description="Описание",
        developer="Dev Co",
        publisher="Pub Co",
        esrb_rating="M",
        cover_path="/provider/7/2/x.jpg",
        genres=["Action"],
        platforms=platforms,
        raw={"n": n},
    )


def plat(slug: str, metascore: int | None, *, lead: bool = False) -> PlatformInfo:
    return PlatformInfo(slug=slug, name=slug.upper(), is_lead=lead, metascore=metascore)


# alpha стоит на двух платформах намеренно: фильтр по PLAT_B не должен её удвоить.
FIXTURE = [
    (product(1, f"{MARK} Alpha", [plat(PLAT_A, 90, lead=True), plat(PLAT_B, 88)]), 9.0),
    (product(2, f"{MARK} Beta", [plat(PLAT_A, 70, lead=True)]), None),
    (product(3, f"{MARK} Gamma 50% off", [plat(PLAT_A, None, lead=True)]), 6.0),
    (product(4, f"{MARK} Delta", [plat(PLAT_B, 80, lead=True)]), 8.0),
]


@pytest.fixture(autouse=True)
async def games_fixture():
    """Чистый стол до и после теста; движок разбирается — см. tests/test_upsert.py."""
    try:
        await delete_test_rows()
    except (OSError, sqlalchemy.exc.OperationalError) as exc:
        pytest.skip(f"Postgres недоступен: {exc}")
    for prod, userscore in FIXTURE:
        lead = prod.lead_platform
        scores = {lead.slug: ScoreStats(score=userscore, count=10, sentiment=None)} if lead else {}
        await upsert_game(prod, scores)
    try:
        yield
        await delete_test_rows()
    finally:
        await dispose_engine()


async def delete_test_rows() -> None:
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM games WHERE id >= 9000100000"))


def titles(page: repo_games.GamesPage) -> list[str]:
    return [g["title"] for g in page.games]


async def test_search_finds_by_substring():
    page = await repo_games.list_games(q=MARK.lower())  # регистр не важен: ILIKE
    assert page.total == 4
    assert {t.split()[1] for t in titles(page)} == {"Alpha", "Beta", "Gamma", "Delta"}


async def test_search_escapes_like_wildcards():
    """`%` из строки поиска — это символ, а не «любой текст»."""
    page = await repo_games.list_games(q="50% off")
    assert titles(page) == [f"{MARK} Gamma 50% off"]


async def test_platform_filter_narrows_and_does_not_duplicate():
    page_a = await repo_games.list_games(platform=PLAT_A, q=MARK)
    assert sorted(titles(page_a)) == sorted([f"{MARK} Alpha", f"{MARK} Beta", f"{MARK} Gamma 50% off"])

    # Alpha есть на обеих платформах — в списке она всё равно одна.
    page_b = await repo_games.list_games(platform=PLAT_B, q=MARK)
    assert sorted(titles(page_b)) == sorted([f"{MARK} Alpha", f"{MARK} Delta"])
    assert page_b.total == 2


async def test_sort_metascore_desc_nulls_last():
    page = await repo_games.list_games(q=MARK, sort="metascore")
    assert [g["best_metascore"] for g in page.games] == [90, 80, 70, None]


async def test_sort_userscore_desc_nulls_last():
    page = await repo_games.list_games(q=MARK, sort="userscore")
    assert [g["best_userscore"] for g in page.games][:3] == [9.0, 8.0, 6.0]
    assert page.games[-1]["best_userscore"] is None


async def test_unknown_sort_falls_back_to_default():
    assert repo_games.normalize_sort("; DROP TABLE games") == repo_games.DEFAULT_SORT


async def test_pagination_splits_and_clamps():
    first = await repo_games.list_games(q=MARK, sort="metascore", page=1, per_page=3)
    assert first.pages == 2 and len(first.games) == 3

    second = await repo_games.list_games(q=MARK, sort="metascore", page=2, per_page=3)
    assert second.page == 2 and len(second.games) == 1
    assert titles(first) + titles(second) == titles(
        await repo_games.list_games(q=MARK, sort="metascore", per_page=10)
    )

    # Страница за пределами набора отдаёт последнюю, а не пустоту.
    beyond = await repo_games.list_games(q=MARK, sort="metascore", page=99, per_page=3)
    assert beyond.page == 2 and titles(beyond) == titles(second)


async def test_platforms_for_filter_include_fixture():
    slugs = {p["platform_slug"] for p in await repo_games.list_platforms()}
    assert {PLAT_A, PLAT_B} <= slugs


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_fragment_has_no_layout_and_pushes_canonical_url(client):
    async with client as http:
        resp = await http.get("/games/fragment", params={"q": MARK, "sort": "metascore"})
    assert resp.status_code == 200
    assert "<html" not in resp.text  # фрагмент, а не целая страница
    assert resp.headers["HX-Push-Url"] == f"/games?q={MARK}&sort=metascore"
    assert "/game/zzqtest-1" in resp.text


async def test_fragment_reports_empty_result(client):
    async with client as http:
        resp = await http.get("/games/fragment", params={"q": "заведомо-нет-такой-игры"})
    assert "Ничего не найдено" in resp.text


async def test_game_card_and_404(client):
    async with client as http:
        ok = await http.get("/game/zzqtest-1")
        missing = await http.get("/game/zzqtest-нет")
    assert ok.status_code == 200
    assert f"{MARK} Alpha" in ok.text
    assert PLAT_A.upper() in ok.text and PLAT_B.upper() in ok.text  # все платформы
    assert missing.status_code == 404
