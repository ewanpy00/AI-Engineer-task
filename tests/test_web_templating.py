"""Хелперы шаблонов, на которых держатся две границы безопасности веб-слоя.

`external_url` решает, можно ли подставить чужую ссылку в `src`/`href`:
автоэкранирование Jinja держит значение внутри атрибута, но схему не
проверяет, а `javascript:` исполняется в origin нашей страницы.

`error_kind` решает, что видно на открытой всем `/status`: причина отказа —
да, текст исключения с хостами и параметрами запросов — нет.
"""

from __future__ import annotations

import pytest

from app.web.templating import UNKNOWN_REASON, error_kind, external_url


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jwplayer.com/players/X.html",
        "http://example.com/video",
        "https://www.youtube.com/watch?v=abc123",
    ],
)
def test_web_urls_pass_through_unchanged(url):
    assert external_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.domain)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)  ",  # схема ищется после strip, пробелы не спасают
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
    ],
)
def test_script_bearing_schemes_are_dropped(url):
    assert external_url(url) is None


def test_missing_url_stays_missing():
    assert external_url(None) is None
    assert external_url("") is None


def test_error_kind_keeps_only_the_reason():
    assert error_kind("MetacriticError: 500 https://backend.metacritic.com/x?y=1") == (
        "MetacriticError"
    )
    assert error_kind("no_session: YA300_SESSION_ID не задан") == "no_session"


def test_error_kind_keeps_an_http_code_as_the_reason():
    """Отказ LLM приходит с кодом вместо имени класса (`llm_error`).

    Квота бесплатного тира — самая частая причина отказа модели (OQ-1), и на
    странице она должна читаться как `429`, а не как безымянная «ошибка».
    """
    assert error_kind("429: RESOURCE_EXHAUSTED. {'error': {'status': ...}}") == "429"
    assert error_kind("llm_disabled: 429: RESOURCE_EXHAUSTED") == "llm_disabled"
    assert error_kind("9999: не HTTP-код") == UNKNOWN_REASON


def test_error_kind_hides_text_without_a_machine_reason():
    """Всё, что не похоже на имя причины, наружу не выносим целиком."""
    assert error_kind("connect to user@10.0.0.5:5432 failed") == UNKNOWN_REASON
    assert error_kind("10.0.0.5:5432 connection refused") == UNKNOWN_REASON
    assert error_kind("<b>boom</b>") == UNKNOWN_REASON


def test_error_kind_on_empty_input():
    assert error_kind(None) == ""
    assert error_kind("") == ""
