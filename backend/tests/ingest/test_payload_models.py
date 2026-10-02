"""Payload model tests for the Intervals.icu ingest models (defect fix).

The real Intervals.icu API returns activity ids as strings with an ``i``
prefix (verified against the live API: e.g. ``i163428838``; the official
API cookbook uses ``GET /activity/i55751783/file``). ``Activity.id`` must
therefore be ``str`` and must tolerate a defensive integer input by
coercing it to ``str``.
"""

from __future__ import annotations

from app.ingest.models import Activity

_REAL_ACTIVITY_PAYLOAD: dict[str, object] = {
    "id": "i163428838",
    "type": "Ride",
    "name": "Evening ride",
    "start_date": "2026-01-15T07:30:00+00:00",
    "start_date_local": "2026-01-15T08:30:00+01:00",
    "moving_time": 3600,
    "elapsed_time": 3900,
    "distance": 42500.0,
    "total_elevation_gain": 320.0,
    "icu_training_load": 142,
    "source": "Garmin Fenix 5",
    "device_name": "Garmin Fenix 5",
    "trainer": False,
}


def test_real_shaped_activity_payload_validates() -> None:
    """A payload shaped like the live API response validates into Activity."""
    activity = Activity.model_validate(dict(_REAL_ACTIVITY_PAYLOAD))

    assert activity.id == "i163428838"
    assert activity.type == "Ride"
    assert activity.name == "Evening ride"
    assert activity.start_date_local == "2026-01-15T08:30:00+01:00"
    assert activity.distance == 42500.0
    assert activity.moving_time == 3600
    # Unknown/extra fields are preserved (extra="allow").
    extras = activity.model_extra or {}
    assert extras["icu_training_load"] == 142
    assert extras["elapsed_time"] == 3900


def test_integer_activity_id_is_coerced_to_string() -> None:
    """A defensive integer id in the payload coerces to a string id."""
    payload = dict(_REAL_ACTIVITY_PAYLOAD)
    payload["id"] = 163428838

    activity = Activity.model_validate(payload)

    assert activity.id == "163428838"
    assert isinstance(activity.id, str)
