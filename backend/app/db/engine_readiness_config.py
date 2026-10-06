"""Settings→engine mapping for the readiness/intensity/durability constants
(RID-10).

MAPPING HELPER ONLY — nothing in ``app/`` consumes readiness, intensity or
durability constants yet (Feature 6 will wire the ``get_readiness`` and
``get_intensity_distribution`` tools and the analysis flow), so this module
deliberately has NO caller today; it exists so that wiring is a reviewed,
tested mapping instead of an ad-hoc read when Feature 6 lands.

Follows the ZON-11 pattern of ``app.db.zones_config`` (itself following the
LOAD-11 pattern of ``app.db.daily_load``): a pure mapping from the sourced
``engine_*`` Settings fields (each with its source/owner-choice comment) onto
the pure engine's keyword parameters. The engine (``app.engine.readiness``,
``app.engine.intensity``, ``app.engine.durability``) keeps its documented
module-constant fallbacks and never imports settings (§6 purity).

A SEPARATE module rather than an extension of ``app.db.zones_config``:
zones_config maps Feature 4's zone/threshold constants; this module maps
Feature 5's readiness/intensity/durability constants. One mapping helper per
engine feature keeps each single-purpose, with its own parsers and its own
pinned tests (``tests/db/test_engine_readiness_config_settings.py``).

The structured string fields (the per-modality threshold maps, the
sport→modality map, the reference-band tuples) are parsed and VALIDATED here
with clear ``ValueError`` s — a malformed environment variable fails loudly,
never silently.
"""

from __future__ import annotations

import math
from typing import Any, cast

from app.core.settings import Settings
from app.engine.intensity import (
    INTENSITY_MODALITY_KEYS,
    SPORT_KEYS,
    IntensityModalityKey,
    ReferenceBands,
    SportKey,
)

__all__ = [
    "durability_constants_from_settings",
    "intensity_constants_from_settings",
    "parse_reference_bands",
    "parse_sport_modality",
    "parse_threshold_pcts",
    "readiness_constants_from_settings",
]


def _split_pairs(raw: str, *, field: str) -> list[tuple[str, str]]:
    """Split a comma-separated ``key:value`` map into trimmed pairs.

    An empty map or an entry without a ``key:value`` shape raises
    ``ValueError`` naming ``field``.
    """
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    if not entries:
        raise ValueError(
            f"{field} must be a non-empty comma-separated list of "
            f"key:value pairs, got {raw!r}"
        )
    pairs: list[tuple[str, str]] = []
    for item in entries:
        key, sep, value = item.partition(":")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(
                f"{field} entries must be key:value pairs, got {item!r} "
                f"in {raw!r}"
            )
        pairs.append((key.strip(), value.strip()))
    return pairs


def _parse_positive_float(
    text: str, *, field: str, label: str, raw: str
) -> float:
    """One positive, finite float; ``ValueError`` naming ``field`` otherwise."""
    try:
        value = float(text)
    except ValueError:
        raise ValueError(
            f"{field} values must be numbers, got {label} {text!r} in {raw!r}"
        ) from None
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"{field} values must be positive and finite, got {label} "
            f"{value!r} in {raw!r}"
        )
    return value


def parse_threshold_pcts(
    raw: str, *, field: str
) -> dict[IntensityModalityKey, float]:
    """Parse a per-modality threshold map (``bike_power:76,run_hr:90,...``).

    Must contain every modality key of
    :data:`app.engine.intensity.INTENSITY_MODALITY_KEYS`
    (``bike_power``, ``run_hr``, ``bike_hr``, ``swim_pace``) exactly once,
    each with a positive finite percentage. Anything else — empty, missing,
    duplicate or unknown key, wrong separator, non-numeric or non-positive
    value — raises ``ValueError`` naming ``field``.
    """
    pairs = _split_pairs(raw, field=field)
    keys = [key for key, _ in pairs]
    if sorted(keys) != sorted(INTENSITY_MODALITY_KEYS):
        raise ValueError(
            f"{field} must contain exactly the modality keys "
            f"{INTENSITY_MODALITY_KEYS} once each, got {raw!r}"
        )
    values: dict[IntensityModalityKey, float] = {}
    for key, value_text in pairs:
        values[cast(IntensityModalityKey, key)] = _parse_positive_float(
            value_text, field=field, label=f"{key!r}", raw=raw
        )
    return values


def parse_sport_modality(
    raw: str, *, field: str
) -> dict[SportKey, IntensityModalityKey]:
    """Parse the sport→modality map (``run:run_hr,bike:bike_power,...``).

    Must contain every sport key of :data:`app.engine.intensity.SPORT_KEYS`
    (``run``, ``bike``, ``swim``) exactly once, each mapping to a modality
    key of :data:`app.engine.intensity.INTENSITY_MODALITY_KEYS`. Anything
    else raises ``ValueError`` naming ``field``.
    """
    pairs = _split_pairs(raw, field=field)
    keys = [key for key, _ in pairs]
    if sorted(keys) != sorted(SPORT_KEYS):
        raise ValueError(
            f"{field} must contain exactly the sport keys {SPORT_KEYS} once "
            f"each, got {raw!r}"
        )
    mapping: dict[SportKey, IntensityModalityKey] = {}
    for key, modality in pairs:
        if modality not in INTENSITY_MODALITY_KEYS:
            raise ValueError(
                f"{field} values must be modality keys "
                f"{INTENSITY_MODALITY_KEYS}, got {modality!r} for {key!r} "
                f"in {raw!r}"
            )
        mapping[cast(SportKey, key)] = modality
    return mapping


def parse_reference_bands(raw: str, *, field: str) -> ReferenceBands:
    """Parse three ``(lo, hi)`` percentage bands from six comma numbers.

    Exactly six finite numbers forming three pairs with
    ``0 <= lo <= hi <= 100`` — the same shape
    :func:`app.engine.intensity.descriptive_pattern_comparison` validates —
    with a ``ValueError`` naming ``field`` otherwise.
    """
    items = [item.strip() for item in raw.split(",") if item.strip()]
    values: list[float] = []
    for item in items:
        try:
            values.append(float(item))
        except ValueError:
            raise ValueError(
                f"{field} must be comma-separated numbers, got {raw!r} "
                f"(invalid entry {item!r})"
            ) from None
    if len(values) != 6:
        raise ValueError(
            f"{field} must be exactly six numbers (three lo,hi band pairs), "
            f"got {raw!r}"
        )
    bands = tuple(
        (values[i], values[i + 1]) for i in range(0, 6, 2)
    )
    for lo, hi in bands:
        if not (math.isfinite(lo) and math.isfinite(hi)) or not (
            0.0 <= lo <= hi <= 100.0
        ):
            raise ValueError(
                f"{field} bounds must satisfy 0 <= lo <= hi <= 100, got "
                f"{raw!r}"
            )
    return cast(ReferenceBands, bands)


def readiness_constants_from_settings(settings: Settings) -> dict[str, Any]:
    """Map the readiness Settings fields onto the engine's kwargs (RID-10).

    Pure mapping; the returned keys are pinned by the mapping test to be
    accepted keyword parameters of the documented engine functions:

    - ``hrv_window_days`` / ``hrv_baseline_days`` / ``hrv_band_sd`` /
      ``hrv_min_baseline_valid_days`` / ``min_window_valid_fraction`` →
      :func:`app.engine.readiness.hrv_readiness` (window and baseline
      LITERATURE, Plews et al. 2013; 0.5 SD SWC band LITERATURE,
      Kiviniemi et al. 2007; completeness floor and window strictness
      OWNER CHOICE),
    - ``rhr_*`` → :func:`app.engine.readiness.resting_hr_readiness`
      (baseline LITERATURE; band and floor OWNER CHOICE),
    - ``sleep_*`` → :func:`app.engine.readiness.sleep_readiness` (all
      OWNER CHOICE — the brief fixes no sleep window),
    - ``tsb_very_negative_below`` →
      :func:`app.engine.readiness.readiness_assessment` (OWNER CHOICE,
      the brief gives no number).

    No caller yet (Feature 6 will consume this); the mapping exists and is
    tested so the wiring is reviewed, not invented later.
    """
    return {
        "hrv_window_days": settings.engine_hrv_window_days,
        "hrv_baseline_days": settings.engine_hrv_baseline_days,
        "hrv_band_sd": settings.engine_hrv_band_sd,
        "hrv_min_baseline_valid_days": settings.engine_hrv_min_baseline_valid_days,
        "min_window_valid_fraction": settings.engine_min_window_valid_fraction,
        "rhr_baseline_days": settings.engine_rhr_baseline_days,
        "rhr_band_sd": settings.engine_rhr_band_sd,
        "rhr_min_baseline_valid_days": settings.engine_rhr_min_baseline_valid_days,
        "sleep_baseline_days": settings.engine_sleep_baseline_days,
        "sleep_band_sd": settings.engine_sleep_band_sd,
        "sleep_min_baseline_valid_days": (
            settings.engine_sleep_min_baseline_valid_days
        ),
        "tsb_very_negative_below": settings.engine_tsb_very_negative,
    }


def intensity_constants_from_settings(settings: Settings) -> dict[str, Any]:
    """Map the intensity Settings fields onto the engine's shapes (RID-10).

    Pure mapping; the returned keys are pinned by the mapping test:

    - ``first_threshold_pcts`` / ``second_threshold_pcts`` — per-modality
      maps consumed one entry at a time by
      :func:`app.engine.intensity.three_zone_model`
      (``first_threshold_pct`` / ``second_threshold_pct``); first cut
      points OWNER CHOICE, second cut points LITERATURE,
    - ``sport_modality`` — the per-sport source table (the shape of
      :data:`app.engine.intensity.SPORT_MODALITY`; OWNER CHOICE),
    - ``min_pattern_week_seconds`` /
      ``polarized_bands`` / ``pyramidal_bands`` →
      :func:`app.engine.intensity.descriptive_pattern_comparison`
      (bands OWNER-REVIEWABLE around the published point values; minimum
      volume OWNER CHOICE),
    - ``speed_tolerance_mps`` / ``gap_cap_median_multiple`` — the
      pause-aware time-in-zone weighting consumed by
      :func:`app.engine.intensity.moving_weights` (speed tolerance the
      FIT SDK's 0.1 m/s stopped-speed default; gap-cap multiple OWNER
      CHOICE).

    No caller yet (Feature 6 will consume this).
    """
    return {
        "first_threshold_pcts": parse_threshold_pcts(
            settings.engine_first_threshold_pcts,
            field="ENGINE_FIRST_THRESHOLD_PCTS",
        ),
        "second_threshold_pcts": parse_threshold_pcts(
            settings.engine_second_threshold_pcts,
            field="ENGINE_SECOND_THRESHOLD_PCTS",
        ),
        "sport_modality": parse_sport_modality(
            settings.engine_sport_modality, field="ENGINE_SPORT_MODALITY"
        ),
        "min_pattern_week_seconds": settings.engine_min_pattern_week_seconds,
        # Pause rule (time-in-zone weighting; see
        # app.engine.intensity.moving_weights): consumed one pair at a
        # time by the intensity service's weighting of the classified
        # samples. Speed tolerance LITERATURE-ADJACENT (FIT SDK
        # stopped_speed_threshold 0.1 m/s); gap-cap multiple OWNER CHOICE.
        "speed_tolerance_mps": settings.engine_pause_speed_tolerance_mps,
        "gap_cap_median_multiple": (
            settings.engine_pause_gap_cap_median_multiple
        ),
        "polarized_bands": parse_reference_bands(
            settings.engine_polarized_bands, field="ENGINE_POLARIZED_BANDS"
        ),
        "pyramidal_bands": parse_reference_bands(
            settings.engine_pyramidal_bands, field="ENGINE_PYRAMIDAL_BANDS"
        ),
    }


def durability_constants_from_settings(settings: Settings) -> dict[str, Any]:
    """Map the durability Settings fields onto the engine's kwargs (RID-10).

    Pure mapping; the returned keys are pinned by the mapping test:

    - ``reference_band`` / ``max_intensity_drift`` →
      :func:`app.engine.durability.session_decoupling` (Friel 5% band
      LITERATURE; steadiness drift limit OWNER CHOICE),
    - ``min_long_session_seconds`` / ``window_days`` / ``min_sessions`` →
      :func:`app.engine.durability.durability_trend` (all OWNER CHOICE;
      Maunder et al. 2021 motivate the trend, not the operational
      thresholds).

    No caller yet (Feature 6 will consume this).
    """
    return {
        "reference_band": settings.engine_decoupling_reference_band,
        "max_intensity_drift": settings.engine_max_half_intensity_drift,
        "min_long_session_seconds": settings.engine_min_long_session_seconds,
        "window_days": settings.engine_trend_window_days,
        "min_sessions": settings.engine_min_trend_sessions,
    }
