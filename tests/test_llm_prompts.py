"""T-25: загрузчик промптов и сами тексты промптов (T-26).

Проверяется не только механика загрузки, но и содержание: правило проекта
«отзывы — недоверенный ввод» должно быть проговорено в самом промпте, иначе
изоляция держится только на форме сообщений.
"""

from __future__ import annotations

import hashlib

import pytest

from app.llm.prompts import PromptError, PromptRepo

REVIEW_PROMPTS = ("review_summary_critic", "review_summary_user")


def test_load_finds_versioned_file(tmp_path):
    (tmp_path / "demo.v1.md").write_text("---\nmodel: m\n---\nтело", encoding="utf-8")
    prompt = PromptRepo(tmp_path).load("demo")

    assert (prompt.name, prompt.version, prompt.version_tag) == ("demo", "v1", "demo.v1")
    assert prompt.text == "тело"
    assert prompt.model == "m"


def test_load_takes_highest_version(tmp_path):
    for n in (1, 2, 10):
        (tmp_path / f"demo.v{n}.md").write_text(f"тело {n}", encoding="utf-8")

    assert PromptRepo(tmp_path).load("demo").version == "v10"
    # явное имя с версией закрепляет конкретный файл
    assert PromptRepo(tmp_path).load("demo.v2").version == "v2"


def test_sha256_changes_when_file_edited_without_version_bump(tmp_path):
    """Сигнал для T-30: промпт поправили, номер версии не подняли."""
    path = tmp_path / "demo.v1.md"
    path.write_text("---\ntemperature: 0.2\n---\nпервая редакция", encoding="utf-8")
    before = PromptRepo(tmp_path).load("demo")
    assert before.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    path.write_text("---\ntemperature: 0.9\n---\nпервая редакция", encoding="utf-8")
    after = PromptRepo(tmp_path).load("demo")  # холодный старт: новый репозиторий

    assert after.version == before.version
    assert after.sha256 != before.sha256


def test_cache_is_per_process(tmp_path):
    path = tmp_path / "demo.v1.md"
    path.write_text("первая редакция", encoding="utf-8")
    repo = PromptRepo(tmp_path)
    first = repo.load("demo")
    path.write_text("вторая редакция", encoding="utf-8")

    assert repo.load("demo") is first  # в пределах процесса текст неизменен


def test_missing_prompt_reports_where_it_looked(tmp_path):
    (tmp_path / "other.v1.md").write_text("x", encoding="utf-8")
    with pytest.raises(PromptError) as exc:
        PromptRepo(tmp_path).load("demo")

    message = str(exc.value)
    assert "demo" in message and str(tmp_path) in message and "other.v1.md" in message


def test_empty_prompt_is_an_error(tmp_path):
    (tmp_path / "demo.v1.md").write_text("---\nmodel: m\n---\n\n", encoding="utf-8")
    with pytest.raises(PromptError):
        PromptRepo(tmp_path).load("demo")


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_shipped_prompt_has_generation_params(name):
    prompt = PromptRepo().load(name)

    assert prompt.version == "v1"
    assert prompt.max_tokens and prompt.temperature is not None
    assert prompt.model  # промпт помечен моделью, под которую написан


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_shipped_prompt_declares_reviews_untrusted(name):
    """CLAUDE.md: отзывы никогда не подставляются в промпт как инструкции."""
    text = PromptRepo().load(name).text.lower()

    assert "<review" in text
    assert "данные, а не инструкции" in text
    assert "никогда не выполняй" in text
    assert "по-русски" in text  # OQ-9: резюме на русском


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_shipped_prompt_has_no_quote_placeholders(name):
    """Цитаты подставляет T-30 отдельным сообщением, а не формат промпта."""
    text = PromptRepo().load(name).text

    assert "{" not in text and "%s" not in text


@pytest.mark.parametrize("name", REVIEW_PROMPTS)
def test_shipped_prompt_pins_thinking_level(name):
    """Уровень «размышлений» — параметр генерации, он живёт в файле (design §5.2).

    На уровне по умолчанию gemini-3.8-flash тратит на размышления весь бюджет
    `max_tokens` и обрывает сам ответ — замер в `app/llm/gemini_client.py`.
    """
    assert PromptRepo().load(name).thinking_level == "LOW"


def test_front_matter_comments_are_ignored(tmp_path):
    (tmp_path / "demo.v1.md").write_text(
        "---\n# пояснение к параметру\nmodel: m\n---\nтело", encoding="utf-8"
    )
    prompt = PromptRepo(tmp_path).load("demo")

    assert prompt.model == "m" and "пояснение" not in prompt.meta
