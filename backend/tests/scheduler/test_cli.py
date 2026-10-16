"""SCH-10 tests: the on-demand CLI for the four scheduled jobs.

The scheduled jobs are only verifiable when they can be fired without
waiting for a crontab, so the CLI (``python -m app.scheduler.cli <job>``)
is the seam that makes SCH-10 runnable on demand. Pinned contracts:

- **Dispatch**: each job name runs the EXACT function the corresponding
  Celery task body calls (``run_morning_brief_sync`` etc. — no duplicated
  job logic, the same-function-as-task convention), passing through the
  injectable dependency seams (``settings``/``session_factory``/
  ``twilio_client`` for the message jobs, ``client``/``session_factory``
  for the sync).
- **Unknown name**: rejected with a clear message naming the valid jobs —
  both programmatically (:class:`UnknownJobError`) and through the CLI
  surface (exit code 2, the name and the choices on stderr).
- **``--help``**: documents every job and the flags, exits 0, runs nothing.
- **No network / no database needed for the dispatch tests**: the runner
  seams are injected fakes, so a test never builds a real Twilio client
  or engine.

The live end-to-end run against the real stack is reported in the task
handoff, not asserted here: it needs the compose Postgres, real
credentials and the rebuilt containers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.scheduler import cli, morning_brief, post_activity_review, tasks, weekly_report

# Sentinel summaries the fake runners return, so a test can assert the
# CLI hands the runner's summary back untouched.
SYNC_SUMMARY: dict[str, Any] = {"days": 30, "ok": True, "activities_synced": 0}
BRIEF_SUMMARY: dict[str, Any] = {"mode": "freeform", "sent": True, "brief": "x"}
REVIEW_SUMMARY: dict[str, Any] = {"reviewed": 1, "sent": 1, "skipped": 0}
REPORT_SUMMARY: dict[str, Any] = {"mode": "template", "sent": False, "report": "y"}


class Recorder:
    """A fake runner: records the kwargs it was called with."""

    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.summary


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(SYNC_SUMMARY)
    monkeypatch.setattr(cli, "run_sync", recorder)
    return recorder


@pytest.fixture
def fake_brief(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(BRIEF_SUMMARY)
    monkeypatch.setattr(cli, "run_morning_brief_sync", recorder)
    return recorder


@pytest.fixture
def fake_review(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(REVIEW_SUMMARY)
    monkeypatch.setattr(cli, "run_post_activity_review_sync", recorder)
    return recorder


@pytest.fixture
def fake_report(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(REPORT_SUMMARY)
    monkeypatch.setattr(cli, "run_weekly_report_sync", recorder)
    return recorder


# --- dispatch: each name runs the exact function the Celery task calls ----


def test_cli_dispatches_sync_with_days(
    fake_sync: Recorder,
) -> None:
    fake_client: Any = "fake-client"
    fake_factory: Any = "fake-factory"
    summary = cli.run_job("sync", days=7, client=fake_client, session_factory=fake_factory)

    assert summary == SYNC_SUMMARY
    assert fake_sync.calls[0]["args"] == (7, None)
    assert fake_sync.calls[0]["kwargs"]["client"] is fake_client
    assert fake_sync.calls[0]["kwargs"]["session_factory"] is fake_factory


def test_cli_dispatches_morning_brief(
    fake_brief: Recorder,
) -> None:
    fake_settings: Any = "fake-settings"
    fake_factory: Any = "fake-factory"
    fake_twilio: Any = "fake-twilio"
    summary = cli.run_job(
        "morning_brief",
        athlete_id=2,
        settings=fake_settings,
        session_factory=fake_factory,
        twilio_client=fake_twilio,
    )

    assert summary == BRIEF_SUMMARY
    call = fake_brief.calls[0]["kwargs"]
    assert call["athlete_id"] == 2
    assert call["settings"] is fake_settings
    assert call["session_factory"] is fake_factory
    assert call["twilio_client"] is fake_twilio


def test_cli_dispatches_post_activity_review(
    fake_review: Recorder,
) -> None:
    fake_twilio: Any = "fake-twilio"
    summary = cli.run_job("post_activity_review", athlete_id=3, twilio_client=fake_twilio)

    assert summary == REVIEW_SUMMARY
    call = fake_review.calls[0]["kwargs"]
    assert call["athlete_id"] == 3
    assert call["twilio_client"] is fake_twilio


def test_cli_dispatches_weekly_report(
    fake_report: Recorder,
) -> None:
    fake_twilio: Any = "fake-twilio"
    summary = cli.run_job("weekly_report", athlete_id=4, twilio_client=fake_twilio)

    assert summary == REPORT_SUMMARY
    call = fake_report.calls[0]["kwargs"]
    assert call["athlete_id"] == 4
    assert call["twilio_client"] is fake_twilio


def test_dispatch_targets_are_the_celery_task_bodies() -> None:
    """The CLI must not duplicate job logic: its runners ARE the functions
    the Celery task bodies call (same-function-as-task, LOAD-10/WA-3)."""
    assert cli.run_sync is tasks.run_sync
    assert cli.run_morning_brief_sync is morning_brief.run_morning_brief_sync
    assert (
        cli.run_post_activity_review_sync
        is post_activity_review.run_post_activity_review_sync
    )
    assert cli.run_weekly_report_sync is weekly_report.run_weekly_report_sync


# --- unknown name ----------------------------------------------------------


def test_run_job_rejects_unknown_name_naming_the_valid_jobs() -> None:
    with pytest.raises(cli.UnknownJobError) as excinfo:
        cli.run_job("daily_briefing")

    message = str(excinfo.value)
    assert "daily_briefing" in message
    for valid in ("sync", "morning_brief", "post_activity_review", "weekly_report"):
        assert valid in message


def test_main_rejects_unknown_name_with_exit_2_and_no_run(
    fake_brief: Recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["daily_briefing"])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "daily_briefing" in stderr
    assert "morning_brief" in stderr  # the valid choices are shown
    assert fake_brief.calls == []  # nothing ran


# --- --help ----------------------------------------------------------------


def test_help_documents_every_job_and_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for job in ("sync", "morning_brief", "post_activity_review", "weekly_report"):
        assert job in out
    assert "--athlete-id" in out
    assert "--days" in out


# --- the CLI surface: JSON summary on stdout, exit 0 ------------------------


def test_main_prints_json_summary_and_exits_zero(
    fake_brief: Recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["morning_brief", "--athlete-id", "2"])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == BRIEF_SUMMARY
    assert fake_brief.calls[0]["kwargs"]["athlete_id"] == 2


def test_main_passes_days_to_the_sync(
    fake_sync: Recorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["sync", "--days", "7"])

    assert exit_code == 0
    json.loads(capsys.readouterr().out)  # well-formed JSON on stdout
    assert fake_sync.calls[0]["args"] == (7, None)


def test_main_defaults_are_the_scheduled_defaults(
    fake_sync: Recorder,
) -> None:
    """With no flags the CLI runs exactly what the beat schedule runs."""
    cli.main(["sync"])

    assert fake_sync.calls[0]["args"] == (30, None)
