"""Settings loading behaviour for local ``.env`` files.

Regression guard: the README tells developers to copy ``.env.example`` to
``.env``, and that template leaves optional numeric keys empty (the owner's
run threshold speed and weight are explicit gaps). An empty value must mean
"not configured" — the field keeps its ``None`` default — instead of raising
a pydantic validation error that breaks every import of ``Settings``.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings


def test_empty_env_values_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty optional numeric env values are ignored, not fatal."""
    monkeypatch.setenv("ATHLETE_WEIGHT_KG", "")
    monkeypatch.setenv("ATHLETE_THRESHOLD_RUN_SPEED_MPS", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.athlete_weight_kg is None
    assert settings.athlete_threshold_run_speed_mps is None


def test_non_empty_env_values_are_still_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real value keeps being parsed into its typed field."""
    monkeypatch.setenv("ATHLETE_LTHR_BPM", "169")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.athlete_lthr_bpm == 169.0


def test_morning_brief_owner_choices_have_documented_defaults() -> None:
    """SCH-5/SCH-6 OWNER CHOICES (§11, §14): the brief's send time defaults
    to 07:30 Europe/Madrid (strictly after the 06:55 daily-load recompute)
    and the out-of-window template SID defaults to EMPTY — the WhatsApp
    sandbox has no approved templates, so the honest default is the skip
    branch, never a send that cannot work."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.morning_brief_time == "07:30"
    assert settings.morning_brief_template_sid == ""


def test_review_and_weekly_owner_choices_have_documented_defaults() -> None:
    """SCH-7/SCH-8 OWNER CHOICES (§11, §14): the review pass's look-back
    window bounds a first backfill away from message spam (48 h is
    generous — a new activity always arrives via a sync well inside it);
    the Sunday report defaults to 18:00 Europe/Madrid; both new messages'
    out-of-window template SIDs default to EMPTY — the skip branch, never
    a send that cannot work."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.post_activity_review_lookback_hours == 48
    assert settings.post_activity_review_template_sid == ""
    assert settings.weekly_report_time == "18:00"
    assert settings.weekly_report_template_sid == ""
