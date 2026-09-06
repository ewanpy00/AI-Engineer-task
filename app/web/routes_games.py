"""Публичные страницы: список игр и карточка."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.web.repo_games import (
    DEFAULT_SORT,
    get_game,
    list_games,
    list_platforms,
    normalize_page,
    normalize_sort,
)
from app.web.templating import query_string, templates

router = APIRouter()


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/games", status_code=307)


async def _list_context(q: str, platform: str, sort: str, page: int) -> dict[str, Any]:
    """Общий контекст для страницы и для HTMX-фрагмента: один источник данных."""
    q, platform = q.strip(), platform.strip()
    sort, page = normalize_sort(sort), normalize_page(page)
    result = await list_games(q=q, platform=platform, sort=sort, page=page)
    return {
        "games": result.games,
        "total": result.total,
        "page": result.page,      # мог съехать вниз, если страниц стало меньше
        "pages": result.pages,
        "q": q,
        "platform": platform,
        "sort": sort,
        "all_platforms": await list_platforms(),
    }


def canonical_url(ctx: dict[str, Any], page: int | None = None) -> str:
    """Адрес страницы списка с текущими параметрами — то, что видит пользователь.

    Значения по умолчанию в строку не попадают: `/games` и
    `/games?sort=new&page=1` — один и тот же экран.
    """
    page = ctx["page"] if page is None else page
    return "/games" + query_string(
        q=ctx["q"] or None,
        platform=ctx["platform"] or None,
        sort=None if ctx["sort"] == DEFAULT_SORT else ctx["sort"],
        page=None if page == 1 else page,
    )


@router.get("/games")
async def games_list(
    request: Request,
    q: str = "",
    platform: str = "",
    sort: str = DEFAULT_SORT,
    page: int = Query(1, ge=1),
):
    return templates.TemplateResponse(request, "games_list.html", await _list_context(q, platform, sort, page))


@router.get("/games/fragment")
async def games_fragment(
    request: Request,
    q: str = "",
    platform: str = "",
    sort: str = DEFAULT_SORT,
    page: int = Query(1, ge=1),
):
    """Тот же список без layout — цель HTMX-подмены."""
    ctx = await _list_context(q, platform, sort, page)
    # htmx ходит на /games/fragment, а в адресной строке должен остаться /games
    # с теми же параметрами: сам он канонического адреса не знает, отдаём заголовком.
    return templates.TemplateResponse(
        request, "_games_fragment.html", ctx, headers={"HX-Push-Url": canonical_url(ctx)}
    )


@router.get("/game/{slug}")
async def game_card(request: Request, slug: str):
    game = await get_game(slug)
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")
    # Резюме отзывов (T-31), похожие игры (T-34) и летсплей (T-46) — отдельные
    # блоки той же страницы, места под них размечены в шаблоне.
    return templates.TemplateResponse(request, "game_card.html", {"game": game})
