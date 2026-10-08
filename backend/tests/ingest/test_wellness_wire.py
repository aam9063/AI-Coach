"""Regression tests: Wellness must map the REAL Intervals.icu wire names.

The field names here come from a live ``GET /athlete/{id}/wellness``
response (captured 2026-10-08, HTTP 200, 22 records; the union of keys
across all records was verified). The previous mappings (``RHR``, a bare
``sleep_minutes``, an ``lnHrv`` extra) were inferred from the API cookbook
and are NOT on the wire — resting HR, sleep duration and ln(HRV) were
silently persisted as NULL.

Wire contract (live-verified 2026-10-08):

- ``hrv``        rMSSD in MILLISECONDS (not ln-transformed)
- ``hrvSDNN``    SDNN in milliseconds (separate metric — never a
                 substitute for rMSSD when deriving ln(hrv))
- ``restingHR``  resting heart rate in bpm
- ``sleepSecs``  sleep duration in SECONDS (stored/consumed value is
                 MINUTES; conversion happens only in the model)
- ``sleepScore`` Intervals' sleep score (0-100)
- ``weight``     body weight in kg

``lnHrv`` is NOT on the wire; ln(hrv) must be derived from the genuine
rMSSD value (``ln(hrv)``), never from SDNN and never left NULL while
``hrv`` is present.
"""

from __future__ import annotations

import math

import pytest

from app.ingest.models import Wellness

# Keys taken verbatim from the live wellness response (subset relevant to
# our consumers, plus representative unmapped fields). ``hrvSDNN`` is a
# DIFFERENT metric from ``hrv`` (rMSSD) — 48.2 vs 65.5 here makes any
# accidental SDNN-based ln() derivation detectable.
_REAL_WELLNESS_PAYLOAD: dict[str, object] = {
    "id": "2026-10-08",
    "hrv": 65.5,  # rMSSD, ms
    "hrvSDNN": 48.2,  # SDNN, ms
    "restingHR": 52,  # bpm
    "sleepSecs": 25200,  # SECONDS
    "sleepScore": 78,
    "sleepQuality": 4,
    "avgSleepingHR": 48.1,
    "weight": 78.5,  # kg
    "readiness": 71,
    "steps": 11234,
    "vo2max": 51.0,
    "baevskySI": 65.0,
    "tempRestingHR": 51.5,
    "soreness": 3,
    "fatigue": 4,
    "mood": 3,
    "motivation": 4,
    "injury": False,
    "stress": 60,
    # Intervals' own PMC values (non-authoritative cross-checks, §5.1).
    "ctl": 71.2,
    "atl": 55.3,
}


class TestWellnessRealWireNames:
    def test_resting_hr_maps_from_resting_hr(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        assert wellness.resting_hr == 52.0

    def test_sleep_minutes_converted_from_sleep_secs(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        # 25200 seconds = 420 minutes; the wire unit is SECONDS, the
        # stored/consumed unit MINUTES.
        assert wellness.sleep_minutes == 420

    def test_sleep_score_maps_from_sleep_score(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        assert wellness.sleep_score == 78.0

    def test_hrv_and_weight_map_by_name(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        assert wellness.hrv == 65.5
        assert wellness.weight == 78.5

    def test_hrv_sdnn_kept_separate_from_rmssd(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        assert wellness.hrv_sdnn == 48.2

    def test_ln_hrv_derived_from_rmssd_never_sdnn(self) -> None:
        wellness = Wellness.model_validate(dict(_REAL_WELLNESS_PAYLOAD))
        assert wellness.ln_hrv is not None
        assert wellness.ln_hrv == pytest.approx(math.log(65.5))
        # Must NOT be ln(hrvSDNN) = ln(48.2) ≈ 3.875.
        assert wellness.ln_hrv != pytest.approx(math.log(48.2))

    def test_ln_hrv_none_only_without_hrv(self) -> None:
        payload = dict(_REAL_WELLNESS_PAYLOAD)
        del payload["hrv"]
        wellness = Wellness.model_validate(payload)
        assert wellness.hrv is None
        assert wellness.ln_hrv is None

    def test_non_positive_hrv_yields_no_ln(self) -> None:
        payload = dict(_REAL_WELLNESS_PAYLOAD)
        payload["hrv"] = 0.0
        wellness = Wellness.model_validate(payload)
        assert wellness.ln_hrv is None


class TestWellnessDefensiveAliases:
    """The old (cookbook-inferred) names stay accepted, wire name wins."""

    def test_legacy_rhr_alias_still_accepted(self) -> None:
        wellness = Wellness.model_validate({"id": "2026-10-08", "RHR": 51})
        assert wellness.resting_hr == 51.0

    def test_resting_hr_beats_legacy_rhr(self) -> None:
        wellness = Wellness.model_validate(
            {"id": "2026-10-08", "restingHR": 52, "RHR": 99}
        )
        assert wellness.resting_hr == 52.0

    def test_populate_by_name_still_works(self) -> None:
        wellness = Wellness.model_validate(
            {"id": "2026-10-08", "resting_hr": 50, "sleep_minutes": 400}
        )
        assert wellness.resting_hr == 50.0
        assert wellness.sleep_minutes == 400
