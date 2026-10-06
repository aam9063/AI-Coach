"""RED tests for FIT parsing with fitdecode (ODD task ING-4, brief §5.2/§6).

Covers:
- parsing the real fixture ``garmin-fenix-5-bike.fit`` into per-second stream
  arrays (time, hr, speed, altitude, distance present; power/cadence absent)
  with ``None`` fill for missing values;
- robustness: unknown message types ignored, missing fields -> None arrays,
  invalid bytes -> ``ValueError`` (no crash);
- swim ``length`` message extraction, unit-tested with fitdecode-like dict
  stubs (the fixture contains no swim data; no network access needed).

Fixture origin and license: see tests/fixtures/README.md.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from app.ingest.fit_parser import (
    STREAM_KEYS,
    Message,
    SwimLength,
    messages_to_stream_lists,
    messages_to_swim_lengths,
    parse_fit_streams,
    parse_fit_swim_lengths,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "garmin-fenix-5-bike.fit"

# The fixture contains exactly 19 `record` messages (verified with fitdecode).
FIXTURE_RECORD_COUNT = 19
FIXTURE_FIRST_TIMESTAMP_EPOCH = 1497283762.0  # 2017-06-12T16:09:22Z


@pytest.fixture
def fit_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def stub_message(name: str, fields: dict[str, object]) -> Message:
    """Build a fitdecode-like message dict for the pure extraction functions."""
    return Message(name=name, fields=fields)


# ---------------------------------------------------------------------------
# Fixture-based stream parsing
# ---------------------------------------------------------------------------


def test_streams_contain_all_stream_keys(fit_bytes: bytes) -> None:
    streams = parse_fit_streams(fit_bytes)
    assert set(streams) == set(STREAM_KEYS)


def test_all_streams_have_one_value_per_record(fit_bytes: bytes) -> None:
    streams = parse_fit_streams(fit_bytes)
    for key, values in streams.items():
        assert len(values) == FIXTURE_RECORD_COUNT, f"stream {key} length mismatch"


def test_time_is_epoch_seconds_and_increasing(fit_bytes: bytes) -> None:
    raw = parse_fit_streams(fit_bytes)["time"]
    time = [t for t in raw if t is not None]
    assert len(time) == FIXTURE_RECORD_COUNT  # no None entries
    assert time[0] == FIXTURE_FIRST_TIMESTAMP_EPOCH
    assert all(a < b for a, b in pairwise(time))


def test_heart_rate_fully_populated(fit_bytes: bytes) -> None:
    hr = parse_fit_streams(fit_bytes)["hr"]
    assert all(v is not None for v in hr)
    assert all(isinstance(v, (int, float)) for v in hr)


def test_distance_monotonic_non_decreasing(fit_bytes: bytes) -> None:
    raw = parse_fit_streams(fit_bytes)["distance"]
    distance = [v for v in raw if v is not None]
    assert len(distance) == FIXTURE_RECORD_COUNT  # no None entries
    assert distance[0] == 0.0
    assert all(a <= b for a, b in pairwise(distance))


def test_speed_and_altitude_present(fit_bytes: bytes) -> None:
    streams = parse_fit_streams(fit_bytes)
    assert any(v is not None for v in streams["speed"])
    assert any(v is not None for v in streams["altitude"])


def test_absent_fields_are_all_none_not_crash(fit_bytes: bytes) -> None:
    """The Fenix 5 bike fixture has no power or cadence fields."""
    streams = parse_fit_streams(fit_bytes)
    assert streams["power"] == [None] * FIXTURE_RECORD_COUNT
    assert streams["cadence"] == [None] * FIXTURE_RECORD_COUNT


def test_unknown_message_types_are_ignored(fit_bytes: bytes) -> None:
    """The fixture contains device_info/unknown_233/... frames; parsing must
    only consume `record` messages (hence exactly the record count)."""
    streams = parse_fit_streams(fit_bytes)
    assert len(streams["hr"]) == FIXTURE_RECORD_COUNT


def test_swim_lengths_empty_for_bike_fixture(fit_bytes: bytes) -> None:
    assert parse_fit_swim_lengths(fit_bytes) == []


# ---------------------------------------------------------------------------
# Robustness on invalid input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [b"", b"garbage" * 32])
def test_invalid_bytes_raise_value_error(bad: bytes) -> None:
    with pytest.raises(ValueError, match="not a valid FIT file"):
        parse_fit_streams(bad)


def test_no_record_messages_yield_empty_lists() -> None:
    """A FIT file with no records (e.g. settings-only) yields empty streams."""
    messages = [stub_message("file_id", {"time_created": 1})]
    streams = messages_to_stream_lists(messages)
    assert streams == {key: [] for key in STREAM_KEYS}


# ---------------------------------------------------------------------------
# Pure extraction logic over fitdecode-like message stubs (no fixture needed)
# ---------------------------------------------------------------------------


def test_streams_from_stub_messages_with_subsets_of_fields() -> None:
    messages = [
        stub_message("record", {"timestamp": 100.0, "heart_rate": 90}),
        stub_message("record", {"timestamp": 101.0, "power": 250, "cadence": 85}),
        stub_message("record", {"timestamp": 102.0}),
    ]
    streams = messages_to_stream_lists(messages)
    assert streams["time"] == [100.0, 101.0, 102.0]
    assert streams["hr"] == [90, None, None]
    assert streams["power"] == [None, 250, None]
    assert streams["cadence"] == [None, 85, None]
    assert streams["speed"] == [None, None, None]
    assert streams["altitude"] == [None, None, None]
    assert streams["distance"] == [None, None, None]


def test_enhanced_fields_preferred_over_plain() -> None:
    messages = [
        stub_message(
            "record",
            {
                "enhanced_speed": 8.1,
                "speed": 8.1,
                "enhanced_altitude": 100.5,
                "altitude": 100.5,
            },
        ),
        stub_message("record", {"speed": 7.2, "altitude": 101.0}),
    ]
    streams = messages_to_stream_lists(messages)
    assert streams["speed"] == [8.1, 7.2]
    assert streams["altitude"] == [100.5, 101.0]


def test_record_messages_with_unknown_fields_are_tolerated() -> None:
    messages = [
        stub_message("record", {"unknown_88": 100, "heart_rate": 77}),
        stub_message("device_info", {"battery_status": "ok"}),
        stub_message("unknown_233", {"foo": 1}),
    ]
    streams = messages_to_stream_lists(messages)
    assert streams["hr"] == [77]
    assert len(streams["time"]) == 1


# ---------------------------------------------------------------------------
# Swim length extraction (stub-based; fixture has no swim data)
# ---------------------------------------------------------------------------


def test_swim_lengths_extracted_in_order_from_length_messages() -> None:
    messages = [
        stub_message(
            "length",
            {
                "start_time": 1000.0,
                "total_timer_time": 25.5,
                "total_elapsed_time": 26.0,
                "swim_stroke": 0,
            },
        ),
        stub_message(
            "length",
            {
                "start_time": 1026.0,
                "total_timer_time": 24.8,
                "total_elapsed_time": 25.0,
                "swim_stroke": 2,
            },
        ),
        stub_message("record", {"timestamp": 1051.0, "heart_rate": 120}),  # ignored
    ]
    lengths = messages_to_swim_lengths(messages)
    assert lengths == [
        SwimLength(
            start_time=1000.0,
            total_timer_time=25.5,
            total_elapsed_time=26.0,
            swim_stroke="freestyle",
        ),
        SwimLength(
            start_time=1026.0,
            total_timer_time=24.8,
            total_elapsed_time=25.0,
            swim_stroke="breaststroke",
        ),
    ]


def test_swim_length_with_missing_fields_is_none_filled() -> None:
    lengths = messages_to_swim_lengths([stub_message("length", {"event": "length"})])
    assert lengths == [
        SwimLength(
            start_time=None,
            total_timer_time=None,
            total_elapsed_time=None,
            swim_stroke=None,
        )
    ]


def test_swim_stroke_unknown_numeric_value_maps_to_none() -> None:
    lengths = messages_to_swim_lengths([stub_message("length", {"swim_stroke": 99})])
    assert lengths[0].swim_stroke is None


def test_swim_stroke_string_value_passes_through() -> None:
    lengths = messages_to_swim_lengths([stub_message("length", {"swim_stroke": "butterfly"})])
    assert lengths[0].swim_stroke == "butterfly"


def test_parse_fit_swim_lengths_uses_length_messages_only(fit_bytes: bytes) -> None:
    """Full parse path: the bike fixture has `event` messages but no `length`
    messages, so no lengths and no crash."""
    assert parse_fit_swim_lengths(fit_bytes) == []
