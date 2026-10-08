"""Engine-backed read tools for the WhatsApp agent (WA-6, brief §9.3).

**What is the SDK's and what is ours**: the tool-calling LOOP (deciding
when to call, executing, feeding results back) is the Strands Agents SDK's;
ours is the tool set itself. Each tool is a THIN wrapper — DB read (outside
the pure engine, which never imports DB or settings code, §6) → engine
output → typed result — and **never computes a number itself** (§3): every
number it returns is an engine output, either read from the persisted
engine tables (``daily_load``, ``readiness_snapshot``, ``weekly_intensity``,
``session_durability``, ``athlete_profile``) or produced by calling the pure
zone functions of :mod:`app.engine.zones` over the stored thresholds.

**Result contract** (WA-6/WA-7 seed, §9.3): every result carries

- ``status``: ``"ok"``, ``"insufficient_data"`` or ``"error"``;
- ``engine_version`` and ``computed_at`` taken from the persisted engine row
  that backs the answer (§6 provenance; settings/now only when there is no
  row);
- ``coverage``: how many days/weeks/sessions/signals backed the answer, so
  the agent can say what it is based on;
- on ``insufficient_data``: a ``detail`` naming WHAT is missing and HOW to
  get it (e.g. "no wellness data recorded; connect a device that syncs
  HRV/sleep to Intervals.icu, then run ``python -m app.db.engine_outputs``")
  — never a zero, never a guessed value (§9.3).

**Docstrings are the model-facing schema**: the ``@tool`` decorator derives
the JSON schema from the signature's type hints and hands the docstring to
the model, so each docstring below states exactly what the tool returns,
its units, and what it does when data is missing. The docstring is part of
the behaviour.

**Security constraint that comes with the SDK** (ODD task, "Agent runtime:
Strands Agents SDK"): the SDK can load tools from a directory
(``load_tools_from_directory``) and a companion ``strands-agents-tools``
package ships file-editing, shell and HTTP tools. The agent must be given
an EXPLICIT list containing only this project's tools — never the directory
loader, never the vended tools — otherwise the model could execute commands
on the host. :func:`tool_list` is the single place that list is built; the
pipeline passes exactly what it returns.

**Session/settings seams**: tools run inside the SDK loop (one Celery task
per message), so they resolve their DB session factory through
:func:`set_session_factory_provider` — the pipeline installs the message's
session factory there, and tests install one bound to the dedicated test
database. With no provider installed, a short-lived engine is created per
tool call from the configured DSN (the single-athlete load makes this
acceptable). The same holds for settings via :func:`set_settings_override`
(inert in production; tests use it to stay independent of the developer's
``.env``).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from strands.tools import tool

from app.core.settings import Settings, get_settings
from app.db.engine_readiness_config import readiness_constants_from_settings
from app.db.models import (
    ActivityRow,
    AthleteProfileRow,
    DailyLoadRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
    WeeklyIntensityRow,
)
from app.db.repository import upsert_subjective_log
from app.db.session import create_db_engine, make_session_factory
from app.db.zones_config import parse_swim_boundaries
from app.engine.zones import hr_zones, power_zones, swim_zones
from app.services.readiness import recompute_readiness

SessionFactory = async_sessionmaker[AsyncSession]

# --- Injectable seams (production defaults derive from configuration) ------

_provider: Callable[[], SessionFactory] | None = None
_settings_override: Settings | None = None


def set_session_factory_provider(provider: Callable[[], SessionFactory]) -> None:
    """Install the session factory the tools read the engine outputs with.

    The pipeline calls this once per inbound message with the message's own
    factory; tests install a factory bound to the dedicated test database.
    """
    global _provider
    _provider = provider


def reset_session_factory_provider() -> None:
    """Remove the installed session-factory provider (tests)."""
    global _provider
    _provider = None


def set_settings_override(settings: Settings) -> None:
    """Pin the settings the tools read (tests; production uses get_settings)."""
    global _settings_override
    _settings_override = settings


def reset_settings_override() -> None:
    """Remove the pinned settings (tests)."""
    global _settings_override
    _settings_override = None


def _settings() -> Settings:
    return _settings_override if _settings_override is not None else get_settings()


@asynccontextmanager
async def tool_session() -> AsyncIterator[AsyncSession]:
    """One session on the installed factory (or a short-lived engine)."""
    provider = _provider
    if provider is not None:
        factory = provider()
        async with factory() as session:
            yield session
        return
    settings = _settings()
    engine = create_db_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


# --- Shared result helpers --------------------------------------------------

def _now(settings: Settings) -> datetime:
    return datetime.now(UTC)


def _today_local(settings: Settings) -> date:
    """Today in the owner's timezone (daily load is a local-time concept)."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _provenance(row: Any | None, settings: Settings) -> tuple[str, str]:
    """``(engine_version, computed_at ISO)`` from a persisted engine row,
    falling back to the configured engine version and now (§6)."""
    if row is not None:
        stamp = getattr(row, "computed_at", None) or getattr(row, "updated_at", None)
        if stamp is not None:
            return row.engine_version, stamp.isoformat()
    return settings.engine_version, _now(settings).isoformat()


# --- date_range parsing -----------------------------------------------------

_RANGE_DAYS = re.compile(r"^(\d+)d$")
_RANGE_EXPLICIT = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")

_RANGE_FORMAT_HELP = (
    "date_range must be a trailing window like '7d' or '30d', or an "
    "explicit inclusive range 'YYYY-MM-DD/YYYY-MM-DD'."
)


def _parse_date_range(date_range: str, *, today: date) -> tuple[date, date] | None:
    """Parse ``"7d"`` / ``"2026-09-01/2026-09-30"`` into ``(start, end)``."""
    match = _RANGE_DAYS.match(date_range.strip())
    if match:
        days = int(match.group(1))
        if days <= 0:
            return None
        return today - timedelta(days=days - 1), today
    match = _RANGE_EXPLICIT.match(date_range.strip())
    if match:
        try:
            start = date.fromisoformat(match.group(1))
            end = date.fromisoformat(match.group(2))
        except ValueError:
            return None
        if start > end:
            return None
        return start, end
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
async def get_load_status(date_range: str = "7d", sport: str | None = None) -> dict[str, Any]:
    """Current training-load status: CTL (fitness), ATL (fatigue) and TSB
    (form = CTL - ATL) as computed by the deterministic engine, for a date
    range, optionally per sport.

    Returns on success (status "ok"): ``ctl``/``atl``/``tsb`` (dimensionless
    TSS-based points, one decimal), the ``date`` they are as of (ISO), the
    day-by-day ``series`` (each entry: date, tss, ctl, atl, tsb for that
    day and sport), ``coverage`` (``window_days`` requested vs
    ``days_with_data`` actually backed by engine rows, plus the ``sport``),
    ``engine_version`` and ``computed_at`` of the persisted daily_load row
    the values come from.

    If there are no engine rows in the window, returns
    ``status="insufficient_data"`` with a ``detail`` naming what is missing
    (synced activities) and how to get it — never a zero. An unparseable
    ``date_range`` returns ``status="error"`` with the accepted formats.

    Args:
        date_range: Trailing window ending today ("7d", "30d") or an
            explicit inclusive range "YYYY-MM-DD/YYYY-MM-DD".
        sport: Optional sport filter ("ride", "run", "swim"); omit for the
            combined all-sports series.
    """
    settings = _settings()
    window = _parse_date_range(date_range, today=_today_local(settings))
    if window is None:
        return {
            "status": "error",
            "detail": _RANGE_FORMAT_HELP,
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }
    window_start, window_end = window
    sport_key = (sport or "combined").strip().lower()

    async with tool_session() as session:
        rows = (
            (
                await session.execute(
                    select(DailyLoadRow)
                    .where(
                        DailyLoadRow.athlete_id == 1,
                        DailyLoadRow.date >= window_start,
                        DailyLoadRow.date <= window_end,
                        DailyLoadRow.sport == sport_key,
                    )
                    .order_by(DailyLoadRow.date)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return {
            "status": "insufficient_data",
            "detail": (
                f"no daily_load rows for sport '{sport_key}' between "
                f"{window_start.isoformat()} and {window_end.isoformat()}: the "
                "engine has not computed training load for this window. "
                "Connect your device so activities sync to Intervals.icu, then "
                "run `python -m app.db.daily_load` to compute CTL/ATL/TSB from "
                "the synced activities."
            ),
            "coverage": {
                "window_days": (window_end - window_start).days + 1,
                "days_with_data": 0,
                "sport": sport_key,
            },
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    latest = rows[-1]
    engine_version, computed_at = _provenance(latest, settings)
    return {
        "status": "ok",
        "sport": sport_key,
        "date": latest.date.isoformat(),
        "ctl": latest.ctl,
        "atl": latest.atl,
        "tsb": latest.tsb,
        "series": [
            {
                "date": row.date.isoformat(),
                "tss": row.tss,
                "ctl": row.ctl,
                "atl": row.atl,
                "tsb": row.tsb,
            }
            for row in rows
        ],
        "coverage": {
            "window_days": (window_end - window_start).days + 1,
            "days_with_data": len(rows),
            "sport": sport_key,
        },
        "engine_version": engine_version,
        "computed_at": computed_at,
    }


_SPORT_ZONE_MODELS: dict[str, str] = {
    "ride": "bike_power",
    "bike": "bike_power",
    "run": "run_hr",
    "swim": "swim_pace",
}

_THRESHOLD_LABELS: dict[str, str] = {
    "bike_power": "FTP",
    "run_hr": "LTHR",
    "swim_pace": "CSS",
}

_SETTINGS_THRESHOLD_FIELDS: dict[str, tuple[str, str]] = {
    "bike_power": ("athlete_ftp_w", "ATHLETE_FTP_W"),
    "run_hr": ("athlete_lthr_bpm", "ATHLETE_LTHR_BPM"),
    "swim_pace": ("athlete_css_speed_mps", "ATHLETE_CSS_SPEED_MPS"),
}


def _insufficient_zones(
    settings: Settings, *, model: str, env_var: str
) -> dict[str, Any]:
    label = _THRESHOLD_LABELS[model]
    return {
        "status": "insufficient_data",
        "detail": (
            f"no {label} (lactate/functional threshold) is configured, so the "
            f"{model} zone table cannot be built — never guessed. Set "
            f"{env_var} (owner configuration, §14): run "
            "`python -m app.ingest.thresholds` to fetch the current values "
            "from Intervals.icu and paste them into your .env."
        ),
        "engine_version": settings.engine_version,
        "computed_at": _now(settings).isoformat(),
    }


@tool
async def get_zones(sport: str) -> dict[str, Any]:
    """The training-zone table for one sport, built by the engine from the
    athlete's stored threshold.

    Returns on success (status "ok"): ``zones`` — the engine's zone table
    with, per zone, ``key`` (Z1..Z7), ``name``, and the absolute bounds
    ``min_value``/``max_value`` plus the percentage bounds
    ``min_pct``/``max_pct``; ``unit`` ("watts" for bike power zones,
    "bpm" for run heart-rate zones, "s_per_100m" for swim pace zones —
    for swims a FASTER pace is a HIGHER intensity); ``zone_model`` (which
    engine table: bike_power, run_hr or swim_pace); ``threshold`` — the
    exact threshold value, unit and source the table was built from;
    ``coverage``; ``engine_version`` and ``computed_at`` from the stored
    athlete profile.

    Bike rides use the Coggan power zones (7 zones, % of FTP), runs the
    Friel run heart-rate zones (7 zones, % of LTHR), swims the CSS pace
    zones (5 zones, % of CSS speed). If the sport's threshold is not
    configured, returns ``status="insufficient_data"`` with a ``detail``
    naming the missing threshold and how to set it — never a guessed table.
    An unknown sport returns ``status="error"`` naming the supported ones.

    Args:
        sport: One of "ride"/"bike", "run" or "swim".
    """
    settings = _settings()
    sport_key = sport.strip().lower()
    model = _SPORT_ZONE_MODELS.get(sport_key)
    if model is None:
        return {
            "status": "error",
            "detail": (
                f"unknown sport {sport!r}: supported sports are ride/bike "
                "(Coggan power zones), run (Friel HR zones) and swim (CSS "
                "pace zones)."
            ),
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    async with tool_session() as session:
        profile = (
            await session.execute(
                select(AthleteProfileRow).where(AthleteProfileRow.athlete_id == 1)
            )
        ).scalar_one_or_none()

    # Threshold resolution: the athlete_profile row is the value of record
    # (with its own provenance); owner configuration (settings) is the
    # fallback for metrics the profile does not store (LTHR) or when no
    # profile exists yet. Missing everywhere -> insufficient (§9.3).
    settings_field, settings_env = _SETTINGS_THRESHOLD_FIELDS[model]
    settings_value: float | None = getattr(settings, settings_field)
    if profile is not None:
        profile_value, profile_source = {
            "bike_power": (profile.ftp_watts, profile.ftp_source),
            "run_hr": (None, None),  # LTHR is not stored in athlete_profile
            "swim_pace": (profile.css_mps, profile.css_source),
        }[model]
    else:
        profile_value, profile_source = None, None

    if profile_value is not None:
        threshold_value, threshold_source = profile_value, profile_source
    elif settings_value is not None:
        threshold_value, threshold_source = settings_value, "owner_configuration"
    else:
        return _insufficient_zones(settings, model=model, env_var=settings_env)

    assert threshold_value is not None  # narrowed above; for mypy
    engine_zones: tuple[Any, ...]
    unit: str
    if model == "bike_power":
        engine_zones = power_zones(threshold_value)
        unit = "watts"
        zones_out = [
            {
                "key": z.key,
                "name": z.name,
                "min_value": z.min_watts,
                "max_value": z.max_watts,
                "min_pct": z.min_pct_ftp,
                "max_pct": z.max_pct_ftp,
            }
            for z in engine_zones
        ]
    elif model == "run_hr":
        engine_zones = hr_zones("run", threshold_value)
        unit = "bpm"
        zones_out = [
            {
                "key": z.key,
                "name": z.name,
                "min_value": z.min_bpm,
                "max_value": z.max_bpm,
                "min_pct": z.min_pct_lthr,
                "max_pct": z.max_pct_lthr,
            }
            for z in engine_zones
        ]
    else:
        engine_zones = swim_zones(
            threshold_value,
            boundary_pcts_css=parse_swim_boundaries(
                settings.engine_swim_zone_boundary_pcts
            ),
        )
        unit = "s_per_100m"
        zones_out = [
            {
                "key": z.key,
                "name": z.name,
                "min_value": z.min_pace_sec_per_100m,
                "max_value": z.max_pace_sec_per_100m,
                "min_pct": z.min_pct_css,
                "max_pct": z.max_pct_css,
            }
            for z in engine_zones
        ]

    engine_version, computed_at = _provenance(profile, settings)
    threshold_unit = {"watts": "watts", "bpm": "bpm", "s_per_100m": "m/s"}[unit]
    return {
        "status": "ok",
        "sport": sport_key,
        "zone_model": model,
        "unit": unit,
        "threshold": {
            "metric": model,
            "value": threshold_value,
            "unit": threshold_unit,
            "source": threshold_source,
        },
        "zones": zones_out,
        "coverage": {
            "threshold_metric": model,
            "zone_count": len(zones_out),
            "source": threshold_source,
        },
        "engine_version": engine_version,
        "computed_at": computed_at,
    }


@tool
async def get_readiness(date: str | None = None) -> dict[str, Any]:
    """The athlete's multi-signal readiness snapshot for one day, exactly
    as the engine assessed it (HRV, resting HR, sleep signals vs their own
    baselines, plus the TSB context) — never a composite score.

    Returns on success (status "ok"): ``signals`` — the persisted engine
    signals in the engine's stable order, each with its ``key``,
    ``status`` (``"ok"`` or ``"insufficient_data"`` with NULL observation),
    observed/baseline values (units as measured: ln(rMSSD) for HRV, bpm for
    resting HR, minutes of sleep) and direction; ``agreement_count`` and
    ``suggest_reduce_intensity`` (the engine's multi-signal warning rule);
    ``suggestion`` and ``reasons`` when the rule fired; the TSB context;
    ``coverage`` (total signals vs which signal keys were
    ``insufficient_data``); ``engine_version`` and ``computed_at`` from the
    persisted snapshot row.

    If no snapshot exists for the date, returns
    ``status="insufficient_data"`` with a ``detail`` naming what is missing
    (wellness data: HRV, sleep, resting HR synced to Intervals.icu, plus
    the engine recompute) and how to get it — never zeros.

    Args:
        date: The day to report (ISO "YYYY-MM-DD"); defaults to today in
            the owner's timezone.
    """
    settings = _settings()
    if date is None:
        day = _today_local(settings)
    else:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            return {
                "status": "error",
                "detail": "date must be ISO 'YYYY-MM-DD'.",
                "engine_version": settings.engine_version,
                "computed_at": _now(settings).isoformat(),
            }

    async with tool_session() as session:
        snapshot = (
            await session.execute(
                select(ReadinessSnapshotRow).where(
                    ReadinessSnapshotRow.athlete_id == 1,
                    ReadinessSnapshotRow.date == day,
                )
            )
        ).scalar_one_or_none()

    if snapshot is None:
        return {
            "status": "insufficient_data",
            "detail": (
                f"no readiness snapshot exists for {day.isoformat()}: readiness "
                "is computed by the engine from wellness data (HRV, sleep, "
                "resting HR). Connect a device that syncs HRV/sleep to "
                "Intervals.icu, then run `python -m app.db.daily_load` and "
                "`python -m app.db.engine_outputs` to compute it."
            ),
            "coverage": {"signals_total": 0, "signals_insufficient": []},
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    insufficient_keys = [
        signal.get("key")
        for signal in snapshot.signals
        if signal.get("status") == "insufficient_data"
    ]
    return {
        "status": "ok",
        "date": snapshot.date.isoformat(),
        "signals": list(snapshot.signals),
        "agreement_count": snapshot.agreement_count,
        "suggest_reduce_intensity": snapshot.suggest_reduce_intensity,
        "suggestion": snapshot.suggestion,
        "reasons": list(snapshot.reasons),
        "tsb": snapshot.tsb,
        "coverage": {
            "signals_total": len(snapshot.signals),
            "signals_insufficient": insufficient_keys,
        },
        "engine_version": snapshot.engine_version,
        "computed_at": snapshot.computed_at.isoformat(),
    }


@tool
async def get_activity_analysis(activity_id: int | str = "last") -> dict[str, Any]:
    """One activity's summary plus its aerobic-durability engine output
    (EF per half and Pa:HR-style decoupling).

    Returns on success (status "ok"): ``activity`` — id, sport, name,
    start_time (ISO), duration_s (seconds), distance_m (metres) and the
    owner-entered ``rpe`` (1-10, null when not entered); ``durability`` —
    the persisted engine result for the session: ``status`` ("ok" or
    "not_steady"), ``ef_first_half``/``ef_second_half`` (output/HR-style
    efficiency factor, dimensionless), ``decoupling`` (fraction) and
    ``decoupling_pct`` (%), ``within_reference_band`` against the engine's
    reference band (5%), ``n_samples`` and the engine's ``detail``; when no
    durability row exists the durability entry is
    ``status="insufficient_data"`` with a ``detail`` naming what is missing;
    ``coverage`` (``durability_samples``); ``engine_version`` and
    ``computed_at`` from the persisted durability row when present.

    If the id is unknown or no activity has been ingested yet, returns
    ``status="insufficient_data"`` with a ``detail`` naming it and how
    activities get in (Intervals.icu sync + ingest).

    Args:
        activity_id: The numeric activity id (as a number or string), or
            "last" for the most recent activity.
    """
    settings = _settings()
    async with tool_session() as session:
        if isinstance(activity_id, str) and activity_id.strip().lower() == "last":
            activity = (
                await session.execute(
                    select(ActivityRow)
                    .order_by(ActivityRow.start_time.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        else:
            try:
                numeric_id = int(activity_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "detail": (
                        "activity_id must be a numeric id or 'last'."
                    ),
                    "engine_version": settings.engine_version,
                    "computed_at": _now(settings).isoformat(),
                }
            activity = (
                await session.execute(
                    select(ActivityRow).where(ActivityRow.id == numeric_id)
                )
            ).scalar_one_or_none()

    if activity is None:
        return {
            "status": "insufficient_data",
            "detail": (
                f"no activity with id {activity_id!r} has been ingested. "
                "Activities arrive from Intervals.icu: connect your device so "
                "sessions sync, then run the ingest to store them."
            ),
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    async with tool_session() as session:
        durability = (
            await session.execute(
                select(SessionDurabilityRow).where(
                    SessionDurabilityRow.activity_id == activity.id
                )
            )
        ).scalar_one_or_none()

    if durability is not None:
        durability_out: dict[str, Any] = {
            "status": durability.status,
            "ef_first_half": durability.ef_first_half,
            "ef_second_half": durability.ef_second_half,
            "decoupling": durability.decoupling,
            "decoupling_pct": durability.decoupling_pct,
            "within_reference_band": durability.within_reference_band,
            "n_samples": durability.n_samples,
            "detail": durability.detail,
        }
        engine_version, computed_at = _provenance(durability, settings)
        durability_samples = durability.n_samples
    else:
        durability_out = {
            "status": "insufficient_data",
            "detail": (
                f"no durability analysis exists for activity {activity.id}: "
                "aerobic durability (EF, decoupling) is computed for bike/run "
                "sessions with power/pace and HR streams of at least 90 "
                "minutes. Run `python -m app.db.engine_outputs` after the "
                "activity syncs."
            ),
        }
        engine_version, computed_at = _provenance(None, settings)
        durability_samples = 0

    return {
        "status": "ok",
        "activity": {
            "id": activity.id,
            "sport": activity.type,
            "name": activity.name,
            "start_time": activity.start_time.isoformat(),
            "duration_s": activity.duration_s,
            "distance_m": activity.distance_m,
            "rpe": activity.rpe,
        },
        "durability": durability_out,
        "coverage": {"durability_samples": durability_samples},
        "engine_version": engine_version,
        "computed_at": computed_at,
    }


@tool
async def get_intensity_distribution(weeks: int = 4, sport: str | None = None) -> dict[str, Any]:
    """The 3-zone intensity distribution (time in Z1/Z2/Z3) of the last N
    ISO weeks, exactly as the engine classified it from the session
    streams.

    Returns on success (status "ok"): ``weeks`` — one entry per persisted
    engine week: ``iso_year``/``iso_week``/``week_start``, ``sport``,
    ``status`` ("data" or "no_data"), ``z1_seconds``/``z2_seconds``/
    ``z3_seconds`` and ``total_seconds`` (seconds in each zone) and the
    engine's ``percentages`` ([z1, z2, z3] per cent; null for no_data
    weeks). The per-week percentages are the ENGINE outputs verbatim — the
    tool never aggregates or recomputes them. ``coverage`` (``weeks_requested``
    vs ``weeks_with_data`` backed by real sessions, and the ``sport``).
    ``engine_version``/``computed_at`` from the persisted rows.

    If no week in the window has intensity data, returns
    ``status="insufficient_data"`` with a ``detail`` naming what is missing
    (per-second streams) and how to get it — never fabricated percentages.
    A non-positive ``weeks`` returns ``status="error"``.

    Args:
        weeks: How many ISO weeks to report, ending with the current week
            (1-52).
        sport: Optional sport filter ("ride"/"bike", "run", "swim"); omit
            for all sports.
    """
    settings = _settings()
    if weeks <= 0 or weeks > 52:
        return {
            "status": "error",
            "detail": "weeks must be between 1 and 52.",
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    today = _today_local(settings)
    iso_year, iso_week, _ = today.isocalendar()
    window: list[tuple[int, int]] = []
    year, week = iso_year, iso_week
    for _ in range(weeks):
        window.append((year, week))
        week -= 1
        if week == 0:
            year -= 1
            week = date(year, 12, 28).isocalendar()[1]
    window.reverse()

    sport_key = (sport or "").strip().lower() or None
    async with tool_session() as session:
        rows = (
            (
                await session.execute(
                    select(WeeklyIntensityRow).where(
                        WeeklyIntensityRow.athlete_id == 1,
                        WeeklyIntensityRow.iso_year.in_({y for y, _ in window}),
                        WeeklyIntensityRow.iso_week.in_({w for _, w in window}),
                        *(
                            [WeeklyIntensityRow.sport == sport_key]
                            if sport_key is not None
                            else []
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    rows = [row for row in rows if (row.iso_year, row.iso_week) in window]
    rows.sort(key=lambda r: (r.iso_year, r.iso_week, r.sport))

    weeks_with_data = sum(
        1 for row in rows if row.status == "data" and row.percentages is not None
    )
    if weeks_with_data == 0:
        sport_label = sport_key or "any sport"
        return {
            "status": "insufficient_data",
            "detail": (
                f"no weekly_intensity rows with intensity data for {sport_label} "
                f"in the last {weeks} ISO weeks: time-in-zone is computed from "
                "per-second streams (power/pace/HR). Connect your device so "
                "activities sync to Intervals.icu, then run "
                "`python -m app.db.engine_outputs` to compute it."
            ),
            "coverage": {
                "weeks_requested": weeks,
                "weeks_with_data": 0,
                "sport": sport_key or "all",
            },
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    engine_version, computed_at = _provenance(rows[-1], settings)
    return {
        "status": "ok",
        "weeks": [
            {
                "iso_year": row.iso_year,
                "iso_week": row.iso_week,
                "week_start": row.week_start.isoformat(),
                "sport": row.sport,
                "status": row.status,
                "z1_seconds": row.z1_seconds,
                "z2_seconds": row.z2_seconds,
                "z3_seconds": row.z3_seconds,
                "total_seconds": row.total_seconds,
                "percentages": list(row.percentages)
                if row.percentages is not None
                else None,
            }
            for row in rows
        ],
        "coverage": {
            "weeks_requested": weeks,
            "weeks_with_data": weeks_with_data,
            "sport": sport_key or "all",
        },
        "engine_version": engine_version,
        "computed_at": computed_at,
    }


# ---------------------------------------------------------------------------
# log_subjective (WA-6 write half, §9.3/§7.4)
# ---------------------------------------------------------------------------

_SUBJECTIVE_SCALE_HELP = (
    "Accepted values: rpe 1-10 (the athlete's own session RPE, the same "
    "scale as activity.rpe), fatigue 1-10 (1 = no fatigue at all, "
    "10 = extreme fatigue), soreness 1-10 (1 = none, 10 = extreme); "
    "notes is free text. Provide at least one of rpe, fatigue or "
    "soreness — a notes-only report is not logged."
)


def _subjective_validation_error(
    rpe: float | None, fatigue: int | None, soreness: int | None
) -> str | None:
    """The error detail for an empty/out-of-range report, or None.

    The LOAD-12 rule: an invalid owner-entered value is NEVER stored
    silently — the error names the rejected value and the accepted scale.
    """
    if rpe is None and fatigue is None and soreness is None:
        return f"empty report: nothing was recorded. {_SUBJECTIVE_SCALE_HELP}"
    for name, value, low, high in (
        ("rpe", rpe, 1.0, 10.0),
        ("fatigue", fatigue, 1, 10),
        ("soreness", soreness, 1, 10),
    ):
        if value is None:
            continue
        if not low <= value <= high:
            return (
                f"{name} {value} is outside the 1-10 scale; rejected — "
                f"nothing stored. {_SUBJECTIVE_SCALE_HELP}"
            )
    return None


@tool
async def log_subjective(
    rpe: float | None = None,
    fatigue: int | None = None,
    soreness: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record the athlete's own daily report (RPE, fatigue, soreness,
    notes) from the conversation and make it count for today's readiness.

    Scales: ``rpe`` 1-10 (the athlete's own session RPE, the same scale
    as activity.rpe); ``fatigue`` 1-10 (1 = no fatigue at all, 10 =
    extreme fatigue); ``soreness`` 1-10 (1 = none, 10 = extreme);
    ``notes`` is free text. Provide at least one of rpe, fatigue or
    soreness — a notes-only report is not logged. The report is stored
    once per day: logging again the same day UPDATES today's entry
    instead of duplicating it — fields provided in the new call
    overwrite, fields omitted keep their earlier value (a later RPE
    note never erases the fatigue reported that morning).

    Readiness effect (§7.4): a reported fatigue — ANY level on the scale
    — becomes the engine's "subjective fatigue" adverse signal for
    today, so it can combine with TSB or the HRV/resting-HR/sleep
    signals in the two-signal warning rule (e.g. TSB very negative +
    reported fatigue → the engine suggests reducing intensity). RPE,
    soreness and notes are recorded for the owner's history but do not
    enter the readiness rule.

    Returns on success (status "ok"): ``date`` (the report's day, in the
    owner's timezone), ``recorded`` (exactly what was stored), and
    ``readiness`` — what it changes: ``status="updated"`` with the
    recomputed snapshot's ``agreement_count``,
    ``suggest_reduce_intensity``, ``adverse_signal_keys`` and
    ``subjective_fatigue_reported``; or ``status="not_assessed"`` with a
    ``detail`` naming what is missing (no daily_load row for today, so
    the TSB input is unavailable) and how to get it. ``coverage``
    reports the recomputed readiness signals; ``engine_version`` is the
    version that stamped the recompute and ``computed_at`` the recompute
    time.

    An out-of-range value or an empty report returns ``status="error"`
    naming the rejected value and the accepted scales — nothing is
    stored (never a silent write).

    Args:
        rpe: Optional session RPE, 1-10.
        fatigue: Optional reported fatigue, 1-10 (1 = none, 10 = extreme).
        soreness: Optional reported soreness, 1-10 (1 = none, 10 = extreme).
        notes: Optional free-text notes from the athlete.
    """
    settings = _settings()
    validation_error = _subjective_validation_error(rpe, fatigue, soreness)
    if validation_error is not None:
        return {
            "status": "error",
            "detail": validation_error,
            "engine_version": settings.engine_version,
            "computed_at": _now(settings).isoformat(),
        }

    day = _today_local(settings)
    computed_at = _now(settings)
    async with tool_session() as session:
        await upsert_subjective_log(
            session,
            athlete_id=1,
            date=day,
            rpe=rpe,
            fatigue=fatigue,
            soreness=soreness,
            notes=notes,
            recorded_at=computed_at,
        )
        # The report becomes a readiness signal through the service (the
        # same pipeline the engine-outputs CLI uses): recompute today's
        # snapshot so the reported fatigue reaches the assessment now.
        report = await recompute_readiness(
            session,
            window_end=day,
            days=1,
            engine_version=settings.engine_version,
            **readiness_constants_from_settings(settings),
        )
        snapshot = (
            await session.execute(
                select(ReadinessSnapshotRow).where(
                    ReadinessSnapshotRow.athlete_id == 1,
                    ReadinessSnapshotRow.date == day,
                )
            )
        ).scalar_one_or_none()
        await session.commit()

    recorded = {
        "rpe": rpe,
        "fatigue": fatigue,
        "soreness": soreness,
        "notes": notes,
    }
    if snapshot is not None:
        insufficient_keys = [
            signal.get("key")
            for signal in snapshot.signals
            if signal.get("status") == "insufficient_data"
        ]
        readiness_out: dict[str, Any] = {
            "status": "updated",
            "agreement_count": snapshot.agreement_count,
            "suggest_reduce_intensity": snapshot.suggest_reduce_intensity,
            "adverse_signal_keys": list(snapshot.adverse_signal_keys),
            "subjective_fatigue_reported": snapshot.subjective_fatigue_reported,
        }
        coverage: dict[str, Any] = {
            "readiness_signals_total": len(snapshot.signals),
            "readiness_signals_insufficient": insufficient_keys,
        }
    else:
        readiness_out = {
            "status": "not_assessed",
            "detail": (
                f"the report was recorded for {day.isoformat()}, but the "
                "readiness snapshot could not be recomputed: no daily_load "
                "combined row exists for this date, so the TSB context "
                "input is unavailable. Run `python -m app.db.daily_load` "
                "to compute training load; the report will be included in "
                "the next readiness recompute."
            ),
        }
        coverage = {
            "readiness_signals_total": 0,
            "readiness_signals_insufficient": [],
        }
        assert report.skipped, "a skipped day must be reported by the service"
    return {
        "status": "ok",
        "date": day.isoformat(),
        "recorded": recorded,
        "readiness": readiness_out,
        "coverage": coverage,
        "engine_version": settings.engine_version,
        "computed_at": computed_at.isoformat(),
    }


def tool_list() -> list[Any]:
    """The EXPLICIT tool list handed to the agent.

    Only this project's engine-backed tools, in this list, ever. Never
    ``load_tools_from_directory``; never the ``strands-agents-tools``
    vended tools (shell/file/HTTP). The remaining write tool
    (``propose_threshold_update``) and the feature-delegating tools
    (``predict_race``, ``plot_metric``, ``search_evidence``) join this
    list with their checklist items — never silently, never from a loader.
    """
    return [
        get_load_status,
        get_zones,
        get_readiness,
        get_activity_analysis,
        get_intensity_distribution,
        log_subjective,
    ]
