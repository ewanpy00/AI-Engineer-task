"""Общее Jinja2-окружение и хелперы шаблонов."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi.templating import Jinja2Templates

from app.config import get_settings

_settings = get_settings()

templates = Jinja2Templates(directory=str(_settings.templates_dir))


def cover_url(cover_path: str | None) -> str | None:
    """bucketPath -> прямой URL обложки на CDN Metacritic (research: cover_image_url)."""
    if not cover_path:
        return None
    return f"{_settings.metacritic_cdn_url}/{cover_path.lstrip('/')}"


def query_string(**params: object) -> str:
    """`?a=1&b=2` из непустых параметров; пустая строка, если непустых нет.

    Нужен и шаблонам (ссылки пагинации), и роутам (канонический URL в
    заголовке `HX-Push-Url`), чтобы обе стороны строили адрес одинаково.
    """
    pairs = [(key, str(value)) for key, value in params.items() if value not in (None, "")]
    return "?" + urlencode(pairs) if pairs else ""


templates.env.globals["cover_url"] = cover_url
templates.env.globals["query_string"] = query_string
