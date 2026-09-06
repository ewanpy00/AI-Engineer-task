"""T-23: конфигурация часовой джобы.

Ждать реального срабатывания cron'а в тесте нечестно и медленно, поэтому
проверяется то, что определяет поведение: расписание, защита от наложения
заходов и то, что упавший заход не убивает планировщик.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.scheduler import JOB_ID, MISFIRE_GRACE_S, _hourly, create_scheduler


class StubRunner:
    def __init__(self, boom: bool = False) -> None:
        self.calls: list[str] = []
        self.boom = boom

    async def run(self, trigger: str) -> None:
        self.calls.append(trigger)
        if self.boom:
            raise RuntimeError("upstream is down")


def test_job_runs_hourly_at_zero_minute_utc():
    job = create_scheduler(StubRunner()).get_job(JOB_ID)
    now = datetime(2026, 9, 6, 12, 30, tzinfo=UTC)

    first = job.trigger.get_next_fire_time(None, now)
    second = job.trigger.get_next_fire_time(first, first)

    assert first == datetime(2026, 9, 6, 13, 0, tzinfo=UTC)
    assert second == datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
    assert str(job.trigger.timezone) == "UTC"


def test_job_never_overlaps_itself():
    """Заход длиннее часа не порождает второй, проспанные часы схлопываются в один."""
    job = create_scheduler(StubRunner()).get_job(JOB_ID)
    assert (job.max_instances, job.coalesce) == (1, True)
    assert job.misfire_grace_time == MISFIRE_GRACE_S


async def test_hourly_marks_the_run_as_scheduled():
    runner = StubRunner()
    await _hourly(runner)
    assert runner.calls == ["schedule"]


async def test_hourly_swallows_failures():
    """Исключение из джобы убило бы её, а не только этот заход."""
    await _hourly(StubRunner(boom=True))
