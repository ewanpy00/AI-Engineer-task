"""T-29: JSONL-лог вызовов LLM.

Лог — не удобство, а требование ТЗ и единственный способ предъявить сырые
обращения к модели (11-decisions.md OQ-3, T-48). Поэтому проверяется и состав
полей, и целостность файла при параллельной записи.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from app.llm.jsonl_logger import JsonlLogger

REQUIRED = {
    "ts", "prompt_version", "prompt_sha256", "model", "messages",
    "response", "tokens", "latency_ms", "status", "error", "attempt",
}


def record(**overrides) -> dict:
    base = {
        "prompt_version": "review_summary_user.v1",
        "prompt_sha256": "a" * 64,
        "model": "gemini-3.8-flash",
        "attempt": 1,
        "status": "ok",
        "latency_ms": 1740,
        "tokens": {"input": 1412, "output": 318},
        "error": None,
        "messages": [{"role": "system", "content": "инструкция"},
                     {"role": "user", "content": "<review id=\"1\">текст</review>"}],
        "response": {"liked": ["а"], "disliked": [], "tldr": "итог"},
    }
    return base | overrides


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_writes_one_line_with_all_required_fields(tmp_path):
    logger = JsonlLogger(tmp_path)
    await logger.write(record())

    path = logger.path_for()
    assert path.name == f"{datetime.now(UTC).date().isoformat()}.jsonl"
    (line,) = read_lines(path)
    assert REQUIRED <= set(line)
    assert line["ts"].endswith("Z")
    assert list(line)[:3] == ["ts", "prompt_version", "prompt_sha256"]


async def test_failed_call_is_logged_too(tmp_path):
    logger = JsonlLogger(tmp_path)
    await logger.write(record(status="error", response=None, error="429 RESOURCE_EXHAUSTED"))

    (line,) = read_lines(logger.path_for())
    assert line["status"] == "error" and line["response"] is None
    assert line["error"] == "429 RESOURCE_EXHAUSTED"


async def test_every_retry_is_its_own_line(tmp_path):
    logger = JsonlLogger(tmp_path)
    for attempt in (1, 2, 3):
        await logger.write(record(attempt=attempt, status="error", response=None))

    assert [line["attempt"] for line in read_lines(logger.path_for())] == [1, 2, 3]


async def test_messages_are_stored_whole(tmp_path):
    """design §5.4: `messages` пишутся как отправлены, без усечения."""
    long_quote = "ц" * 5000
    logger = JsonlLogger(tmp_path)
    await logger.write(
        record(messages=[{"role": "system", "content": "инструкция"},
                         {"role": "user", "content": long_quote}])
    )

    (line,) = read_lines(logger.path_for())
    assert line["messages"][1]["content"] == long_quote


async def test_concurrent_writes_do_not_interleave(tmp_path):
    """T-22 гоняет игры под `Semaphore(4)` — строки не должны рваться."""
    logger = JsonlLogger(tmp_path)
    await asyncio.gather(
        *(logger.write(record(game_id=n, messages=[{"role": "user", "content": "щ" * 20_000}]))
          for n in range(30))
    )

    lines = read_lines(logger.path_for())  # json.loads упадёт на порванной строке
    assert sorted(line["game_id"] for line in lines) == list(range(30))


async def test_write_never_raises_on_broken_destination(tmp_path):
    """Лог не должен ронять обход: каталог занят файлом — пишем в никуда."""
    blocked = tmp_path / "occupied"
    blocked.write_text("я файл, а не каталог", encoding="utf-8")

    await JsonlLogger(blocked / "llm").write(record())  # исключения быть не должно
