"""Settings→engine mapping for the zone/threshold constants (ZON-11).

MAPPING HELPER ONLY — nothing in ``app/`` consumes zones yet (Feature 6
will wire the ``get_zones`` tool and the threshold-proposal flow), so this
module deliberately has NO caller today; it exists so that wiring is a
reviewed, tested mapping instead of an ad-hoc read when Feature 6 lands.

Follows the LOAD-11 pattern of ``app.db.daily_load``
(``engine_constants_from_settings``): a pure mapping from the sourced
``engine_*`` Settings fields (each with its source/owner-choice comment)
onto the pure engine's keyword parameters. The engine
(``app.engine.zones``) keeps its documented module-constant fallbacks and
never imports settings (§6 purity).

The comma-separated string ladders (MMP durations, FTP precedence, swim
boundaries) are parsed and VALIDATED here with clear ``ValueError`` s —
a malformed environment variable fails loudly, never silently.
"""

from __future__ import annotations

import itertools
from typing import Any, cast

from app.core.settings import Settings
from app.engine.zones import FTP_SOURCE_KEYS, FtpSourceKey

__all__ = [
    "parse_float_ladder",
    "parse_ftp_precedence",
    "parse_int_ladder",
    "parse_swim_boundaries",
    "zone_constants_from_settings",
]


def _split_ladder(raw: str, *, field: str) -> list[str]:
    """Split a comma-separated ladder into non-empty trimmed entries."""
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        raise ValueError(
            f"{field} must be a non-empty comma-separated list, got {raw!r}"
        )
    return items


def parse_int_ladder(raw: str, *, field: str) -> tuple[int, ...]:
    """Parse a comma-separated positive-integer ladder (MMP durations).

    ``raw`` entries are trimmed; whitespace and a trailing comma are
    tolerated. An empty ladder, a non-integer entry or a non-positive
    value raises ``ValueError`` naming ``field`` — never silently
    truncated or defaulted.
    """
    values: list[int] = []
    for item in _split_ladder(raw, field=field):
        try:
            value = int(item)
        except ValueError:
            raise ValueError(
                f"{field} must be comma-separated integers, got {raw!r} "
                f"(invalid entry {item!r})"
            ) from None
        if value <= 0:
            raise ValueError(
                f"{field} entries must be positive, got {value!r} in {raw!r}"
            )
        values.append(value)
    return tuple(values)


def parse_float_ladder(raw: str, *, field: str) -> tuple[float, ...]:
    """Parse a comma-separated positive-float ladder (swim boundaries).

    Same rules as :func:`parse_int_ladder` with float entries.
    """
    values: list[float] = []
    for item in _split_ladder(raw, field=field):
        try:
            value = float(item)
        except ValueError:
            raise ValueError(
                f"{field} must be comma-separated numbers, got {raw!r} "
                f"(invalid entry {item!r})"
            ) from None
        if value <= 0.0:
            raise ValueError(
                f"{field} entries must be positive, got {value!r} in {raw!r}"
            )
        values.append(value)
    return tuple(values)


def parse_ftp_precedence(
    raw: str, *, field: str = "ENGINE_FTP_PRECEDENCE"
) -> tuple[FtpSourceKey, ...]:
    """Parse an FTP source precedence: a permutation of the source keys.

    ``raw`` must contain every key of :data:`FTP_SOURCE_KEYS`
    (``manual``, ``cp_derived``, ``twenty_min_power``) exactly once, in
    the desired precedence order (first wins). Anything else — duplicate,
    missing or unknown key — raises ``ValueError`` naming ``field``.
    """
    order = tuple(_split_ladder(raw, field=field))
    if sorted(order) != sorted(FTP_SOURCE_KEYS):
        raise ValueError(
            f"{field} must be a comma-separated permutation of the FTP "
            f"source keys {FTP_SOURCE_KEYS}, got {raw!r}"
        )
    return tuple(cast(FtpSourceKey, item) for item in order)


def parse_swim_boundaries(
    raw: str, *, field: str = "ENGINE_SWIM_ZONE_BOUNDARY_PCTS"
) -> tuple[float, float, float, float]:
    """Parse the four swim-zone boundaries (% of CSS speed).

    Exactly four strictly increasing positive values defining five zones —
    the same shape :func:`app.engine.zones.swim_zones` validates — with a
    ``ValueError`` naming ``field`` otherwise.
    """
    values = parse_float_ladder(raw, field=field)
    if len(values) != 4 or any(
        lo >= hi for lo, hi in itertools.pairwise(values)
    ):
        raise ValueError(
            f"{field} must be exactly four strictly increasing positive "
            f"boundaries defining five swim zones, got {raw!r}"
        )
    return (values[0], values[1], values[2], values[3])


def zone_constants_from_settings(settings: Settings) -> dict[str, Any]:
    """Map the zone/threshold Settings fields onto the engine's kwargs (ZON-11).

    Pure mapping; the returned keys are pinned by the mapping test to be
    accepted keyword parameters of the documented engine functions:

    - ``cp_min_duration_s`` / ``cp_max_duration_s`` →
      :func:`app.engine.zones.fit_critical_power` (2-20 min CP window,
      LITERATURE),
    - ``mmp_durations_s`` → :func:`app.engine.zones.mean_maximal_power_curve`
      (MMP ladder, OWNER CHOICE),
    - ``cs_min_duration_s`` / ``cs_max_duration_s`` →
      :func:`app.engine.zones.fit_critical_speed` (~3-20 min CS window,
      LITERATURE),
    - ``ftp_precedence`` / ``cp_to_ftp_factor`` /
      ``twenty_min_to_ftp_factor`` → :func:`app.engine.zones.resolve_ftp`
      (precedence OWNER CHOICE; factor 1.0 convention + OWNER CHOICE;
      0.95 LITERATURE),
    - ``easy_pct`` … ``repetition_pct`` →
      :func:`app.engine.zones.training_paces` (Daniels bands LITERATURE,
      midpoints OWNER-REVIEWABLE),
    - ``swim_boundary_pcts_css`` → :func:`app.engine.zones.swim_zones` /
      ``swim_zone_for`` (OWNER-REVIEWABLE except the 100% LITERATURE
      anchor),
    - ``threshold_change_margin`` →
      :func:`app.engine.zones.propose_threshold_change` (ZON-9 margin,
      OWNER CHOICE).

    No caller yet (Feature 6 will consume this); the mapping exists and is
    tested so the wiring is reviewed, not invented later.
    """
    return {
        "cp_min_duration_s": settings.engine_cp_fit_window_min_s,
        "cp_max_duration_s": settings.engine_cp_fit_window_max_s,
        "mmp_durations_s": parse_int_ladder(
            settings.engine_mmp_durations_s, field="ENGINE_MMP_DURATIONS_S"
        ),
        "cs_min_duration_s": settings.engine_cs_fit_window_min_s,
        "cs_max_duration_s": settings.engine_cs_fit_window_max_s,
        "ftp_precedence": parse_ftp_precedence(settings.engine_ftp_precedence),
        "cp_to_ftp_factor": settings.engine_cp_to_ftp_factor,
        "twenty_min_to_ftp_factor": settings.engine_twenty_min_to_ftp_factor,
        "easy_pct": settings.engine_daniels_easy_pct_vo2max,
        "marathon_pct": settings.engine_daniels_marathon_pct_vo2max,
        "threshold_pct": settings.engine_daniels_threshold_pct_vo2max,
        "interval_pct": settings.engine_daniels_interval_pct_vo2max,
        "repetition_pct": settings.engine_daniels_repetition_pct_vo2max,
        "swim_boundary_pcts_css": parse_swim_boundaries(
            settings.engine_swim_zone_boundary_pcts
        ),
        "threshold_change_margin": settings.engine_threshold_change_margin,
    }
