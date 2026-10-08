"""RED tests for FIT session-info extraction (ODD tasks FI-3/FI-4).

The immediate analysis of an uploaded FIT file needs two things the stream
parser does not expose: the session's SPORT (the engine's
``ActivityLoadInput`` requires one — unknown sports are rejected, never
guessed) and the session's DURATION. Both live in the FIT ``session``
message (with a ``sport`` message as fallback), so this extraction is part
of the pure parser surface (``app.ingest.fit_parser``): fitdecode-like
dicts for the pure transformation, the real fixture
``garmin-fenix-5-bike.fit`` for the adaptation layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.fit_parser import (
    FitSessionInfo,
    messages_to_session_info,
    parse_fit_session_info,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "garmin-fenix-5-bike.fit"

# Live-verified with fitdecode: the fixture's session message carries
# sport 'cycling', sub_sport 'generic', total_timer_time/total_elapsed_time
# 60.363 s (0.3 Hz sampling, 19 records).
FIXTURE_SPORT = "cycling"
FIXTURE_TOTAL_TIMER_TIME_S = 60.363


def stub_message(name: str, fields: dict[str, object]) -> dict[str, object]:
    return {"name": name, "fields": fields}


@pytest.fixture
def fit_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


class TestMessagesToSessionInfo:
    """Pure extraction over fitdecode-like message dicts."""

    def test_last_session_message_wins(self) -> None:
        messages = [
            stub_message(
                "session",
                {
                    "sport": "cycling",
                    "sub_sport": "generic",
                    "total_timer_time": 10.0,
                    "total_elapsed_time": 12.0,
                },
            ),
            stub_message(
                "session",
                {
                    "sport": "running",
                    "sub_sport": "trail",
                    "total_timer_time": 20.0,
                    "total_elapsed_time": 21.0,
                },
            ),
        ]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.sport == "running"
        assert info.sub_sport == "trail"
        assert info.total_timer_time_s == 20.0
        assert info.total_elapsed_time_s == 21.0

    def test_sport_message_is_the_fallback_when_no_session(self) -> None:
        messages = [
            stub_message("record", {"heart_rate": 100}),
            stub_message("sport", {"sport": "swimming", "sub_sport": "open_water"}),
        ]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.sport == "swimming"
        assert info.sub_sport == "open_water"
        assert info.total_timer_time_s is None
        assert info.total_elapsed_time_s is None

    def test_session_sport_takes_precedence_over_sport_message(self) -> None:
        messages = [
            stub_message("sport", {"sport": "walking"}),
            stub_message("session", {"sport": "cycling", "total_timer_time": 5.0}),
        ]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.sport == "cycling"
        assert info.total_timer_time_s == 5.0

    def test_missing_fields_are_none_never_fabricated(self) -> None:
        messages = [stub_message("session", {"total_timer_time": 30.0})]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.sport is None
        assert info.sub_sport is None
        assert info.total_timer_time_s == 30.0
        assert info.total_elapsed_time_s is None

    def test_non_string_sport_is_none(self) -> None:
        # An unmapped numeric enum value must not surface as an int that
        # downstream code could mistake for a sport string.
        messages = [stub_message("session", {"sport": 77, "total_timer_time": 1.0})]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.sport is None
        assert info.total_timer_time_s == 1.0

    def test_non_numeric_durations_are_none(self) -> None:
        messages = [stub_message("session", {"total_timer_time": "oops"})]
        info = messages_to_session_info(messages)  # type: ignore[arg-type]
        assert info.total_timer_time_s is None


class TestParseFitSessionInfo:
    """Adaptation layer over raw FIT bytes (real fixture)."""

    def test_fixture_session_info(self, fit_bytes: bytes) -> None:
        info = parse_fit_session_info(fit_bytes)
        assert isinstance(info, FitSessionInfo)
        assert info.sport == FIXTURE_SPORT
        assert info.total_timer_time_s == pytest.approx(FIXTURE_TOTAL_TIMER_TIME_S)
        assert info.total_elapsed_time_s == pytest.approx(FIXTURE_TOTAL_TIMER_TIME_S)

    def test_empty_input_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not a valid FIT file"):
            parse_fit_session_info(b"")

    def test_garbage_input_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not a valid FIT file"):
            parse_fit_session_info(b"definitely not a FIT file")
