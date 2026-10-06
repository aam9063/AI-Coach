"""Tests for athlete-threshold extraction and the thresholds CLI (LOAD-11).

Covers:
- ``IntervalsClient.get_sport_settings`` and ``get_athlete_profile`` over an
  ``httpx.MockTransport`` (no network), against the payload shape verified
  against the live API on 2026-10-02;
- the pure ``extract_athlete_thresholds`` mapping per sport (cycling FTP from
  the "Ride" entry, run threshold pace from the "Run" entry, swim CSS speed
  from the "Swim" entry) including misses that stay ``None`` and are reported
  as explicit gaps;
- the ``python -m app.ingest.thresholds`` CLI run in-process with an injected
  fake client: thresholds summary, ready-to-paste .env lines, GAPS list on
  stderr, exit code 0 with gaps, and no file writes anywhere.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.settings import Settings
from app.ingest.client import IntervalsClient
from app.ingest.models import AthleteProfile, SportSettings
from app.ingest.thresholds import (
    AthleteThresholds,
    extract_athlete_thresholds,
)

BASE_URL = "https://intervals.icu/api/v1"
API_KEY = "secret-key"


def make_settings() -> Settings:
    """Build explicit settings so tests never depend on ambient env vars."""
    return Settings(
        intervals_api_key=API_KEY,
        intervals_base_url=BASE_URL,
        intervals_athlete_id="0",
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[IntervalsClient, list[httpx.Request]]:
    """Build a client over a recording MockTransport."""
    sent: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    client = IntervalsClient(
        settings=make_settings(), transport=httpx.MockTransport(recording_handler)
    )
    return client, sent


def relative_path(request: httpx.Request) -> str:
    """Path of a request relative to the configured base URL path (/api/v1)."""
    base = httpx.URL(BASE_URL).path
    assert request.url.path.startswith(base)
    return request.url.path[len(base) :]


# --- Live-verified payload shapes (live API, 2026-10-02) ----------------------

SPORT_SETTINGS_PAYLOAD: list[dict[str, Any]] = [
    {
        "id": 2303813,
        "types": ["Ride", "VirtualRide", "MountainBikeRide", "GravelRide"],
        "ftp": 180,
        "power_zones": [55, 75, 90, 105, 120, 150, 999],
        "lthr": 169,
        "max_hr": 186,
        "hr_zones": [123, 140, 155, 169, 178],
        "threshold_pace": None,
        "pace_zones": None,
    },
    {
        "id": 2303814,
        "types": ["Run", "VirtualRun", "TrailRun"],
        "ftp": None,
        "power_zones": None,
        "lthr": 169,
        "max_hr": 186,
        "hr_zones": [123, 140, 155, 169, 178],
        "threshold_pace": None,
        "pace_zones": None,
    },
    {
        "id": 2303815,
        "types": ["Swim", "OpenWaterSwim"],
        "ftp": None,
        "power_zones": None,
        "lthr": 169,
        "max_hr": 186,
        "hr_zones": [123, 140, 155, 169, 178],
        "threshold_pace": 0.8333333,
        "pace_zones": None,
    },
    {
        "id": 2303816,
        "types": ["Other"],
        "ftp": None,
        "power_zones": None,
        "lthr": None,
        "max_hr": None,
        "hr_zones": None,
        "threshold_pace": None,
        "pace_zones": None,
    },
]

ATHLETE_PAYLOAD: dict[str, Any] = {
    "id": "i555003",
    "name": "Test Owner",
    "sex": "M",
    "icu_resting_hr": 65,
    "weight": None,
}


def make_sport_settings() -> list[SportSettings]:
    return [SportSettings.model_validate(item) for item in SPORT_SETTINGS_PAYLOAD]


def make_athlete() -> AthleteProfile:
    return AthleteProfile.model_validate(ATHLETE_PAYLOAD)


# --- Client endpoints ---------------------------------------------------------


def test_get_sport_settings_sends_get_and_parses_entries() -> None:
    client, sent = make_client(
        lambda _: httpx.Response(200, json=SPORT_SETTINGS_PAYLOAD)
    )

    entries = client.get_sport_settings()

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "GET"
    assert relative_path(request) == "/athlete/0/sport-settings"
    assert len(entries) == 4
    ride = next(e for e in entries if "Ride" in e.types)
    assert ride.ftp == 180
    assert ride.lthr == 169
    assert ride.max_hr == 186
    assert ride.power_zones == [55, 75, 90, 105, 120, 150, 999]
    swim = next(e for e in entries if "Swim" in e.types)
    assert swim.threshold_pace == pytest.approx(0.8333333)


def test_get_athlete_profile_sends_get_and_parses_profile() -> None:
    client, sent = make_client(lambda _: httpx.Response(200, json=ATHLETE_PAYLOAD))

    athlete = client.get_athlete_profile()

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "GET"
    assert relative_path(request) == "/athlete/0"
    assert athlete.id == "i555003"
    assert athlete.name == "Test Owner"
    assert athlete.sex == "M"
    assert athlete.icu_resting_hr == 65
    assert athlete.weight is None


# --- Pure mapping --------------------------------------------------------------


def test_extract_maps_cycling_ftp_from_ride_entry() -> None:
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert thresholds.ftp_w == 180


def test_extract_maps_run_threshold_pace_from_run_entry() -> None:
    # Owner has no run threshold pace in Intervals.icu (null): stays None.
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert thresholds.run_threshold_speed_mps is None


def test_extract_maps_swim_css_speed_from_swim_entry() -> None:
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert thresholds.css_speed_mps == pytest.approx(0.8333333)


def test_extract_maps_lthr_and_max_hr_per_sport() -> None:
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert thresholds.lthr_bike_bpm == 169
    assert thresholds.lthr_run_bpm == 169
    assert thresholds.lthr_swim_bpm == 169
    assert thresholds.hr_max_bike_bpm == 186
    assert thresholds.hr_max_run_bpm == 186
    assert thresholds.hr_max_swim_bpm == 186


def test_extract_maps_resting_hr_and_weight() -> None:
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert thresholds.hr_rest_bpm == 65
    # Weight is a real gap for this owner (null in the live profile).
    assert thresholds.weight_kg is None


def test_extract_reports_gaps_explicitly() -> None:
    thresholds = extract_athlete_thresholds(make_sport_settings(), make_athlete())
    assert "run_threshold_speed_mps" in thresholds.gaps
    assert "weight_kg" in thresholds.gaps
    assert "ftp_w" not in thresholds.gaps
    assert "css_speed_mps" not in thresholds.gaps


def test_extract_missing_sport_entry_stays_none_and_reports_gap() -> None:
    # Only the "Other" entry survives: every sport-specific value is a gap.
    settings_only_other = [
        SportSettings.model_validate(SPORT_SETTINGS_PAYLOAD[3])
    ]
    thresholds = extract_athlete_thresholds(settings_only_other, make_athlete())
    assert thresholds.ftp_w is None
    assert thresholds.run_threshold_speed_mps is None
    assert thresholds.css_speed_mps is None
    assert thresholds.lthr_bike_bpm is None
    assert thresholds.lthr_run_bpm is None
    assert thresholds.lthr_swim_bpm is None
    assert thresholds.hr_max_bike_bpm is None
    assert thresholds.hr_max_run_bpm is None
    assert thresholds.hr_max_swim_bpm is None
    for key in (
        "ftp_w",
        "run_threshold_speed_mps",
        "css_speed_mps",
        "lthr_bike_bpm",
        "lthr_run_bpm",
        "lthr_swim_bpm",
        "hr_max_bike_bpm",
        "hr_max_run_bpm",
        "hr_max_swim_bpm",
    ):
        assert key in thresholds.gaps


def test_extract_missing_athlete_values_stay_none_and_reported() -> None:
    athlete = AthleteProfile.model_validate(
        {"id": "i555003", "name": "X", "sex": None, "icu_resting_hr": None, "weight": None}
    )
    thresholds = extract_athlete_thresholds(make_sport_settings(), athlete)
    assert thresholds.hr_rest_bpm is None
    assert thresholds.weight_kg is None
    assert "hr_rest_bpm" in thresholds.gaps
    assert "weight_kg" in thresholds.gaps


def test_athlete_thresholds_gaps_default_empty() -> None:
    thresholds = AthleteThresholds()
    assert thresholds.gaps == []


# --- Settings defaults ----------------------------------------------------------


def test_settings_athlete_fields_default_to_none() -> None:
    # Assert the DECLARED field defaults instead of instantiating Settings:
    # construction also reads the ambient environment and the developer's local
    # .env, where the owner's real thresholds are seeded (gitignored), so an
    # instance-based assertion would be neither hermetic nor about defaults.
    for name in (
        "athlete_ftp_w",
        "athlete_lthr_bpm",
        "athlete_hr_max_bpm",
        "athlete_hr_rest_bpm",
        "athlete_css_speed_mps",
        "athlete_threshold_run_speed_mps",
        "athlete_weight_kg",
    ):
        assert Settings.model_fields[name].default is None, name


# --- CLI (in-process, injected fake client, no network, no file writes) ---------


class FakeThresholdsClient:
    """Duck-typed client returning the live-verified fixture payloads."""

    def get_sport_settings(self) -> list[SportSettings]:
        return make_sport_settings()

    def get_athlete_profile(self) -> AthleteProfile:
        return make_athlete()


def test_cli_prints_thresholds_env_lines_and_gaps(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from app.ingest import thresholds as thresholds_module

    exit_code = thresholds_module.main(
        [], client=FakeThresholdsClient(), settings=make_settings()
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # Threshold summary on stdout.
    assert "180" in captured.out  # FTP
    assert "0.83" in captured.out  # CSS speed
    assert "169" in captured.out  # LTHR
    assert "65" in captured.out  # resting HR
    # Ready-to-paste .env lines on stdout.
    assert "ATHLETE_FTP_W=180" in captured.out
    assert "ATHLETE_HR_REST_BPM=65" in captured.out
    assert "ATHLETE_WEIGHT_KG=" in captured.out  # missing -> empty value
    assert "ATHLETE_THRESHOLD_RUN_SPEED_MPS=" in captured.out
    # GAPS list on stderr, never silently defaulted.
    assert "GAPS" in captured.err
    assert "run_threshold_speed_mps" in captured.err
    assert "weight_kg" in captured.err
    # No files were written anywhere in the working directory.
    assert list(tmp_path.iterdir()) == []


def test_cli_exits_zero_with_gaps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.ingest import thresholds as thresholds_module

    exit_code = thresholds_module.main(
        [], client=FakeThresholdsClient(), settings=make_settings()
    )
    assert exit_code == 0
    assert "GAPS" in capsys.readouterr().err


def test_cli_does_not_modify_env_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INTERVALS_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INTERVALS_API_KEY", "secret")

    from app.ingest import thresholds as thresholds_module

    exit_code = thresholds_module.main(
        [], client=FakeThresholdsClient(), settings=make_settings()
    )

    assert exit_code == 0
    assert env_file.read_text(encoding="utf-8") == "INTERVALS_API_KEY=secret\n"
    # And nothing else appeared next to it.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
    capsys.readouterr()  # drain output so pytest does not warn
    assert not os.environ.get("ATHLETE_FTP_W")  # no env mutation either
