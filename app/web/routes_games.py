"""Публичные страницы: список игр и карточка."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web.repo_games import list_games
from app.web.templating import templates

router = APIRouter()


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/games", status_code=307)


@router.get("/games")
async def games_list(request: Request):
    # TODO T-15: поиск по названию, фильтр по платформе, сортировка, пагинация
    return templates.TemplateResponse(request, "games_list.html", {"games": await list_games()})
