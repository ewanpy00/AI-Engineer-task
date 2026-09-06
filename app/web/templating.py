"""Общее Jinja2-окружение и хелперы шаблонов."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import get_settings

_settings = get_settings()

templates = Jinja2Templates(directory=str(_settings.templates_dir))


def cover_url(cover_path: str | None) -> str | None:
    """bucketPath -> прямой URL обложки на CDN Metacritic (research: cover_image_url)."""
    if not cover_path:
        return None
    return f"{_settings.metacritic_cdn_url}/{cover_path.lstrip('/')}"


templates.env.globals["cover_url"] = cover_url
