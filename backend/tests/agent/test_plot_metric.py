"""CH-10 tests: the ``plot_metric`` tool (§9.3, Feature 7).

Contract pinned here:

- **Kind registry**: exactly the five supported kinds the ODD tasks name
  (``performance_manager``, ``weekly_tss``, ``time_in_zone``, ``hrv``,
  ``race_prediction``), each mapping to ONE canonical kind string and
  renderer, plus the stub docstring's aliases (``ctl_atl`` ->
  ``performance_manager``, ``intensity`` -> ``time_in_zone``).
- **date_range parsing**: trailing windows ("7d"/"30d"/"90d") ending
  today, or an explicit inclusive "YYYY-MM-DD/YYYY-MM-DD"; every invalid
  form is a CLEAR ERROR RESPONSE — never an exception into the agent loop.
- **Unsupported kinds**: unknown names AND kinds whose renderer exists
  but whose engine data source does not yet (mean-maximal curve, run
  pace/HR + EF trends) return ``status="error"``
  naming what is missing — no chart, number or curve is ever fabricated.
- **Fail closed without media configuration**: without
  ``chart_media_secret``/``chart_public_base_url`` the tool renders
  nothing and returns ``status="error"`` — never an unsigned URL, and it
  must not even reach the database.
- **Typed success result**: ``chart_id`` (int), ``media_url`` — the
  PUBLIC expiring URL built from the signed token (the secret itself
  never appears in it) — ``kind``, truthful ``coverage`` (samples that
  actually back the chart vs the window requested),
  ``engine_version``/``computed_at`` from the persisted engine rows.
- **DB-backed happy paths**: one per supported kind (skip cleanly
  without Postgres, like the other agent tests — see
  ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent import tools
from app.agent.chart_tool import (
    CHART_KIND_ALIASES,
    CHART_RENDERERS,
    SUPPORTED_CHART_KINDS,
)
from app.api.charts import build_chart_media_url, sign_chart_token, verify_chart_token
from app.charts.race_prediction import (
    RacePredictionSample,
    render_race_prediction_breakdown,
)
from app.core.settings import Settings
from app.db.models import (
    DailyLoadRow,
    ReadinessSnapshotRow,
    WeeklyIntensityRow,
    WellnessRow,
)

pytestmark = pytest.mark.anyio

ENGINE_VERSION = "test-engine-1.0"
COMPUTED_AT = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)
CHART_SECRET = "test-chart-secret"
CHART_BASE = "https://charts.example.com"


def real_today_local() -> date:
    """Today in the owner's timezone — the 'today' the tools use — so
    trailing-window seeds cannot drift off the window's edge (the
    test_agent_tools precedent, observed 2026-10-13)."""
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "engine_version": "settings-fallback-1.0",
        "chart_media_secret": CHART_SECRET,
        "chart_public_base_url": CHART_BASE,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def no_media_settings() -> Settings:
    """Settings WITHOUT the chart media configuration (fail-closed tests)."""
    return make_settings(chart_media_secret="", chart_public_base_url="")


@pytest.fixture
def media_settings() -> Iterator[Settings]:
    """Settings WITH the chart media configuration (non-DB error paths)."""
    settings = make_settings()
    tools.set_settings_override(settings)
    yield settings
    tools.reset_settings_override()


@pytest.fixture
async def tool_db(
    db_engine: AsyncEngine, media_settings: Settings
) -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Point the tools' session and settings seams at the test fixtures."""
    factory: async_sessionmaker[Any] = async_sessionmaker(db_engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(media_settings)
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


async def seed(session: AsyncSession, row: Any) -> None:
    session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Seed helpers (engine OUTPUT rows + the wellness rows the HRV chart reads)
# ---------------------------------------------------------------------------

def make_daily_load(
    day: date, *, sport: str = "combined", tss: float = 0.0,
    ctl: float = 40.0, atl: float = 60.0, tsb: float = -20.0,
) -> DailyLoadRow:
    return DailyLoadRow(
        athlete_id=1, date=day, sport=sport, tss=tss, ctl=ctl, atl=atl, tsb=tsb,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


def make_weekly_intensity(
    iso_year: int, iso_week: int, *, sport: str = "bike", status: str = "data",
    seconds: tuple[float, float, float] = (12000.0, 2400.0, 1600.0),
) -> WeeklyIntensityRow:
    percentages = [75.0, 15.0, 10.0] if status == "data" else None
    return WeeklyIntensityRow(
        athlete_id=1, iso_year=iso_year, iso_week=iso_week,
        week_start=date.fromisocalendar(iso_year, iso_week, 1), sport=sport,
        status=status, z1_seconds=seconds[0], z2_seconds=seconds[1],
        z3_seconds=seconds[2], total_seconds=sum(seconds), percentages=percentages,
        engine_version=ENGINE_VERSION, computed_at=COMPUTED_AT,
    )


def make_wellness(day: date, *, ln_hrv: float = 4.1) -> WellnessRow:
    return WellnessRow(athlete_id=1, date=day, hrv=60.0, ln_hrv=ln_hrv)


def make_snapshot(
    day: date, *, baseline_mean: float = 4.0, baseline_sd: float = 0.2,
) -> ReadinessSnapshotRow:
    return ReadinessSnapshotRow(
        athlete_id=1, date=day,
        signals=[
            {
                "key": "hrv_ln_rmssd",
                "status": "ok",
                "observed": 4.05,
                "baseline_mean": baseline_mean,
                "baseline_sd": baseline_sd,
                "band_half_width_sd": 0.5,
                "direction": "in_band",
                "confidence": 1.0,
                "flagged": False,
                "detail": "test signal",
            },
            {
                "key": "resting_hr",
                "status": "insufficient_data",
                "observed": None,
                "baseline_mean": None,
                "baseline_sd": None,
                "direction": None,
                "confidence": None,
            },
        ],
        adverse_signal_keys=[],
        agreement_count=0,
        suggest_reduce_intensity=False,
        suggestion=None,
        reasons=[],
        tsb=-5.0,
        tsb_very_negative_below=-10.0,
        subjective_fatigue_reported=None,
        acwr=None,
        engine_version=ENGINE_VERSION,
        computed_at=COMPUTED_AT,
    )


# ---------------------------------------------------------------------------
# Kind registry
# ---------------------------------------------------------------------------

class TestKindRegistry:
    def test_supported_kinds_are_exactly_the_five_registered_kinds(self) -> None:
        # CH-10 shipped the four window-based kinds; RP-10 added the
        # race_prediction kind wired to Feature 8's distance path.
        assert SUPPORTED_CHART_KINDS == (
            "performance_manager", "weekly_tss", "time_in_zone", "hrv",
            "race_prediction",
        )

    def test_aliases_resolve_to_one_canonical_kind_each(self) -> None:
        # The stub's docstring implied "ctl_atl" and "intensity": both are
        # accepted and map to exactly ONE canonical kind string.
        assert CHART_KIND_ALIASES == {
            "ctl_atl": "performance_manager",
            "intensity": "time_in_zone",
        }
        assert set(CHART_KIND_ALIASES.values()) <= set(SUPPORTED_CHART_KINDS)

    def test_every_supported_kind_has_a_renderer(self) -> None:
        assert set(CHART_RENDERERS) == set(SUPPORTED_CHART_KINDS)


# ---------------------------------------------------------------------------
# date_range parsing (invalid forms are errors, never exceptions)
# ---------------------------------------------------------------------------

class TestDateRangeParsing:
    @pytest.mark.parametrize(
        "date_range",
        [
            "nonsense",
            "",
            "7",
            "d7",
            "0d",
            "-3d",
            "7.5d",
            "2026-09-01",  # explicit form needs BOTH endpoints
            "2026-09-02/2026-09-01",  # start after end
            "2026-13-01/2026-13-02",  # not a real date
            "2026-09-01/2026-13-99",
        ],
    )
    async def test_invalid_range_is_an_error_response_not_an_exception(
        self, media_settings: Settings, date_range: str
    ) -> None:
        # With the media settings configured the range check is the first
        # thing that can fail — no database is touched (no fixture).
        result = await tools.plot_metric(kind="hrv", date_range=date_range)
        assert result["status"] == "error"
        assert "date_range" in result["detail"]
        assert result["chart_id"] is None
        assert result["media_url"] is None


# ---------------------------------------------------------------------------
# Unsupported kinds: never fabricated
# ---------------------------------------------------------------------------

class TestUnsupportedKinds:
    async def test_unknown_kind_is_an_error_naming_the_supported_set(
        self, media_settings: Settings
    ) -> None:
        result = await tools.plot_metric(kind="load", date_range="30d")
        assert result["status"] == "error"
        assert "unknown chart kind" in result["detail"].lower()
        for supported in SUPPORTED_CHART_KINDS:
            assert supported in result["detail"]
        assert result["status"] != "insufficient_data"
        assert result["chart_id"] is None and result["media_url"] is None

    async def test_race_prediction_needs_the_distance_never_guesses_it(
        self, media_settings: Settings
    ) -> None:
        result = await tools.plot_metric(kind="race_prediction", date_range="30d")
        assert result["status"] == "error"
        assert "race distance" in result["detail"].lower()
        assert result["status"] != "insufficient_data"
        assert result["chart_id"] is None and result["media_url"] is None

    async def test_mean_maximal_curve_names_the_missing_engine_source(
        self, media_settings: Settings
    ) -> None:
        result = await tools.plot_metric(kind="mean_maximal_curve", date_range="30d")
        assert result["status"] == "error"
        assert "mean-maximal" in result["detail"].lower()
        assert "engine" in result["detail"].lower()
        assert result["status"] != "insufficient_data"

    @pytest.mark.parametrize("kind", ["run_trends", "ef_trend", "run_pace_hr"])
    async def test_run_trend_kinds_name_the_missing_engine_source(
        self, media_settings: Settings, kind: str
    ) -> None:
        result = await tools.plot_metric(kind=kind, date_range="30d")
        assert result["status"] == "error"
        assert result["status"] != "insufficient_data"
        assert result["chart_id"] is None and result["media_url"] is None


# ---------------------------------------------------------------------------
# Fail closed without the media configuration
# ---------------------------------------------------------------------------

class TestFailClosedWithoutMediaConfig:
    async def test_missing_secret_and_base_renders_nothing_hands_out_nothing(
        self, no_media_settings: Settings
    ) -> None:
        # No DB fixture on purpose: the fail-closed check must fire BEFORE
        # any data is read or rendered. The no-media settings are set as
        # the EXPLICIT override — the ambient .env legitimately carries
        # CHART_* once the media route is configured, and this test must
        # stay hermetic to that.
        tools.set_settings_override(no_media_settings)
        try:
            result = await tools.plot_metric(kind="performance_manager", date_range="7d")
        finally:
            tools.reset_settings_override()
        assert result["status"] == "error"
        assert "media" in result["detail"].lower()
        assert result["chart_id"] is None
        assert result["media_url"] is None

    async def test_secret_without_public_base_is_also_fail_closed(
        self, no_media_settings: Settings
    ) -> None:
        settings = make_settings(chart_public_base_url="")
        tools.set_settings_override(settings)
        try:
            result = await tools.plot_metric(kind="hrv", date_range="7d")
        finally:
            tools.reset_settings_override()
        assert result["status"] == "error"
        assert result["media_url"] is None


# ---------------------------------------------------------------------------
# Media URL builder (pure): signed, expiring, secret never leaks
# ---------------------------------------------------------------------------

class TestBuildChartMediaUrl:
    def test_builds_the_signed_expiring_public_url(self) -> None:
        settings = make_settings()
        # Fixed FUTURE instant: verify_chart_token checks expiry against the
        # real clock, so a past 'now' would be (correctly) reported expired.
        now = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
        url = build_chart_media_url(7, settings, now)
        expected_token = sign_chart_token(
            7,
            now + timedelta(seconds=settings.chart_media_ttl_seconds),
            CHART_SECRET,
        )
        assert url == f"{CHART_BASE}/charts/7?token={expected_token}"
        # The token in the URL verifies back to the chart id and the
        # secret itself never appears in the URL.
        assert verify_chart_token(expected_token, CHART_SECRET) == 7
        assert CHART_SECRET not in (url or "")

    def test_ttl_setting_controls_the_expiry(self) -> None:
        settings = make_settings(chart_media_ttl_seconds=60)
        now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        url = build_chart_media_url(3, settings, now)
        expected_token = sign_chart_token(3, now + timedelta(seconds=60), CHART_SECRET)
        assert url == f"{CHART_BASE}/charts/3?token={expected_token}"

    def test_fail_closed_without_secret_or_base(self) -> None:
        now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        assert build_chart_media_url(1, make_settings(chart_media_secret=""), now) is None
        assert build_chart_media_url(1, make_settings(chart_public_base_url=""), now) is None


# ---------------------------------------------------------------------------
# DB-backed happy paths (skip cleanly without Postgres)
# ---------------------------------------------------------------------------

def assert_ok_chart_shape(result: dict[str, Any], *, kind: str) -> int:
    """The CH-10 success contract, asserted once for every happy path."""
    assert result["status"] == "ok"
    assert result["kind"] == kind
    chart_id = result["chart_id"]
    assert isinstance(chart_id, int)
    media_url = result["media_url"]
    assert isinstance(media_url, str)
    assert media_url.startswith(f"{CHART_BASE}/charts/{chart_id}?token=")
    token = media_url.split("token=", 1)[1]
    assert verify_chart_token(token, CHART_SECRET) == chart_id
    assert CHART_SECRET not in media_url
    coverage = result["coverage"]
    assert coverage["samples_in_chart"] >= 1
    assert coverage["window_days"] >= 1
    assert coverage["window_start"] <= coverage["window_end"]
    assert result["engine_version"] == ENGINE_VERSION
    assert result["computed_at"] == COMPUTED_AT.isoformat()
    return chart_id


class TestPerformanceManagerChart:
    async def test_renders_the_combined_ctl_atl_tsb_rows(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        today = real_today_local()
        async with tool_db() as session:
            for offset in range(3):
                await seed(session, make_daily_load(
                    today - timedelta(days=2 - offset),
                    ctl=40.0 + offset, atl=60.0 + offset, tsb=-20.0 + offset,
                ))

        result = await tools.plot_metric(kind="performance_manager", date_range="7d")

        assert_ok_chart_shape(result, kind="performance_manager")
        assert result["coverage"]["window_days"] == 7
        assert result["coverage"]["samples_in_chart"] == 3
        assert result["coverage"]["sport"] is None

    async def test_alias_ctl_atl_resolves_to_performance_manager(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(real_today_local(), ctl=41.0))

        result = await tools.plot_metric(kind="ctl_atl", date_range="7d")

        assert_ok_chart_shape(result, kind="performance_manager")

    async def test_sport_filter_is_documented_ignored_for_the_combined_view(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(real_today_local(), ctl=41.0))

        result = await tools.plot_metric(
            kind="performance_manager", date_range="7d", sport="ride"
        )

        assert_ok_chart_shape(result, kind="performance_manager")
        assert result["coverage"]["sport"] is None

    async def test_explicit_inclusive_range_is_parsed(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(date(2026, 9, 15), ctl=30.0, atl=50.0,
                                                tsb=-20.0))

        result = await tools.plot_metric(
            kind="performance_manager", date_range="2026-09-01/2026-09-30"
        )

        assert_ok_chart_shape(result, kind="performance_manager")
        assert result["coverage"]["window_start"] == "2026-09-01"
        assert result["coverage"]["window_end"] == "2026-09-30"
        assert result["coverage"]["window_days"] == 30
        assert result["coverage"]["samples_in_chart"] == 1

    async def test_empty_database_is_insufficient_data_never_a_chart(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plot_metric(kind="performance_manager", date_range="30d")

        assert result["status"] == "insufficient_data"
        assert "daily_load" in result["detail"]
        assert "python -m app.db.daily_load" in result["detail"]
        assert result["chart_id"] is None and result["media_url"] is None
        assert result["coverage"]["samples_in_chart"] == 0


class TestWeeklyTssChart:
    async def test_sums_per_sport_tss_into_iso_weeks(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        # 2026-09-07 is the Monday of ISO week 37; seed rides on Mon and
        # Wed of week 37 and one ride in week 38.
        async with tool_db() as session:
            await seed(session, make_daily_load(date(2026, 9, 7), sport="ride", tss=60.0))
            await seed(session, make_daily_load(date(2026, 9, 9), sport="ride", tss=40.0))
            await seed(session, make_daily_load(date(2026, 9, 8), sport="run", tss=30.0))
            await seed(session, make_daily_load(date(2026, 9, 15), sport="ride", tss=100.0))
            # A combined row must NOT count into the per-sport stack.
            await seed(session, make_daily_load(date(2026, 9, 9), tss=130.0))

        result = await tools.plot_metric(
            kind="weekly_tss", date_range="2026-09-01/2026-09-20"
        )

        assert_ok_chart_shape(result, kind="weekly_tss")
        assert result["coverage"]["samples_in_chart"] == 2  # two ISO weeks
        assert result["coverage"]["sport"] is None

    async def test_sport_filter_narrows_the_rows(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_daily_load(date(2026, 9, 7), sport="ride", tss=60.0))
            await seed(session, make_daily_load(date(2026, 9, 8), sport="run", tss=30.0))

        result = await tools.plot_metric(
            kind="weekly_tss", date_range="2026-09-01/2026-09-20", sport="ride"
        )

        assert_ok_chart_shape(result, kind="weekly_tss")
        assert result["coverage"]["samples_in_chart"] == 1
        assert result["coverage"]["sport"] == "ride"

    async def test_unknown_sport_is_an_argument_error(
        self, media_settings: Settings
    ) -> None:
        result = await tools.plot_metric(
            kind="weekly_tss", date_range="30d", sport="quidditch"
        )
        assert result["status"] == "error"
        assert "unknown sport" in result["detail"].lower()

    async def test_empty_database_is_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plot_metric(kind="weekly_tss", date_range="30d")
        assert result["status"] == "insufficient_data"
        assert "daily_load" in result["detail"]


class TestTimeInZoneChart:
    async def test_converts_persisted_zone_seconds_to_minutes_per_week(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_weekly_intensity(2026, 37, sport="bike"))
            await seed(session, make_weekly_intensity(2026, 37, sport="run"))
            await seed(session, make_weekly_intensity(2026, 38, sport="bike"))

        result = await tools.plot_metric(
            kind="time_in_zone", date_range="2026-09-01/2026-09-20"
        )

        assert_ok_chart_shape(result, kind="time_in_zone")
        assert result["coverage"]["samples_in_chart"] == 2

    async def test_alias_intensity_resolves_to_time_in_zone(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_weekly_intensity(2026, 37, sport="bike"))

        result = await tools.plot_metric(
            kind="intensity", date_range="2026-09-01/2026-09-20"
        )

        assert_ok_chart_shape(result, kind="time_in_zone")

    async def test_sport_filter_and_no_data_weeks(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_weekly_intensity(2026, 37, sport="ride"))
            await seed(session, make_weekly_intensity(2026, 38, sport="ride",
                                                      status="no_data"))
            await seed(session, make_weekly_intensity(2026, 37, sport="run"))

        result = await tools.plot_metric(
            kind="time_in_zone", date_range="2026-09-01/2026-09-20", sport="ride"
        )

        assert_ok_chart_shape(result, kind="time_in_zone")
        # The no_data week is reported AS missing, never drawn as data.
        assert result["coverage"]["samples_in_chart"] == 1
        assert result["coverage"]["sport"] == "ride"

    async def test_empty_database_is_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plot_metric(kind="time_in_zone", date_range="30d")
        assert result["status"] == "insufficient_data"
        assert "weekly_intensity" in result["detail"]


class TestHrvChart:
    async def test_daily_ln_rmssd_with_the_snapshot_baseline_band(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        async with tool_db() as session:
            for i, day in enumerate(days):
                await seed(session, make_wellness(day, ln_hrv=4.0 + 0.1 * i))
                await seed(session, make_snapshot(day, baseline_mean=4.0,
                                                  baseline_sd=0.2))

        result = await tools.plot_metric(
            kind="hrv", date_range="2026-09-01/2026-09-30"
        )

        assert_ok_chart_shape(result, kind="hrv")
        assert result["coverage"]["samples_in_chart"] == 3
        assert result["coverage"]["days_with_hrv_value"] == 3

    async def test_days_without_a_baseline_band_are_truthfully_excluded(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_wellness(date(2026, 9, 15), ln_hrv=4.0))
            await seed(session, make_snapshot(date(2026, 9, 15)))
            # ln rMSSD value exists but NO readiness snapshot for this day.
            await seed(session, make_wellness(date(2026, 9, 16), ln_hrv=4.2))

        result = await tools.plot_metric(
            kind="hrv", date_range="2026-09-01/2026-09-30"
        )

        assert_ok_chart_shape(result, kind="hrv")
        assert result["coverage"]["samples_in_chart"] == 1
        assert result["coverage"]["days_with_hrv_value"] == 2

    async def test_sport_filter_is_documented_ignored_for_hrv(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        async with tool_db() as session:
            await seed(session, make_wellness(date(2026, 9, 15), ln_hrv=4.0))
            await seed(session, make_snapshot(date(2026, 9, 15)))

        result = await tools.plot_metric(
            kind="hrv", date_range="2026-09-01/2026-09-30", sport="ride"
        )

        assert_ok_chart_shape(result, kind="hrv")
        assert result["coverage"]["sport"] is None

    async def test_empty_database_is_insufficient_data(
        self, tool_db: async_sessionmaker[Any]
    ) -> None:
        result = await tools.plot_metric(kind="hrv", date_range="30d")
        assert result["status"] == "insufficient_data"
        assert "ln rmssd" in result["detail"].lower() or "hrv" in result["detail"].lower()


# ---------------------------------------------------------------------------
# race_prediction kind: wired to Feature 8's distance path (RP-10 consumer)
# ---------------------------------------------------------------------------

class TestRacePredictionChart:
    async def test_renders_the_prediction_breakdown_from_the_athlete_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The distance path needs NO database: the chart renders from the
        owner-configured athlete inputs over the documented standard flat
        course. The PERSISTENCE seam is stubbed (a fake chart id) — the
        wiring under test is assembler -> renderer -> save -> URL."""
        settings = make_settings(
            athlete_ftp_w=250.0,
            athlete_css_speed_mps=1.8,
            athlete_threshold_run_speed_mps=3.1,
        )
        tools.set_settings_override(settings)

        async def fake_save_chart(*args: Any, **kwargs: Any) -> int:
            return 42

        monkeypatch.setattr(tools, "save_chart", fake_save_chart)
        try:
            result = await tools.plot_metric(
                kind="race_prediction", date_range="30d", sport="olympic"
            )
        finally:
            tools.reset_settings_override()

        assert result["status"] == "ok", result
        assert result["chart_id"] is not None
        assert result["media_url"] is not None
        assert result["coverage"] is not None
        # One sample per predicted segment (swim, bike, run, t1, t2).
        assert result["coverage"]["samples_in_chart"] > 0

    async def test_chart_and_tool_agree_on_the_same_engine_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ONE source of truth: the chart and ``predict_race`` must
        consume the SAME engine output. The engine->tool leg is pinned
        verbatim in tests/agent/test_predict_race.py
        (``assert_prediction_verbatim``); this pins the tool->chart leg:
        the PNG the chart tool saves is byte-identical to the pure
        renderer fed with the tool's own segments — a second source of
        truth could never pass this."""
        settings = make_settings(
            athlete_ftp_w=250.0,
            athlete_css_speed_mps=1.8,
            athlete_threshold_run_speed_mps=3.1,
        )
        tools.set_settings_override(settings)
        captured: dict[str, bytes] = {}

        async def fake_save_chart(
            _session: Any,
            _kind: str,
            png: bytes,
            _engine_version: str,
            **_kwargs: Any,
        ) -> int:
            captured["png"] = png
            return 42

        monkeypatch.setattr(tools, "save_chart", fake_save_chart)
        try:
            tool_result = await tools.predict_race(distance="olympic")
            chart_result = await tools.plot_metric(
                kind="race_prediction", date_range="30d", sport="olympic"
            )
        finally:
            tools.reset_settings_override()

        assert tool_result["status"] == "ok", tool_result
        assert chart_result["status"] == "ok", chart_result
        expected_png = render_race_prediction_breakdown(
            [
                RacePredictionSample(
                    label=segment["segment"],
                    point_seconds=segment["point"],
                    lower_seconds=segment["lower"],
                    upper_seconds=segment["upper"],
                )
                for segment in tool_result["segments"]
            ]
        )
        assert captured["png"] == expected_png
        assert (
            chart_result["coverage"]["samples_in_chart"]
            == len(tool_result["segments"])
        )

    async def test_unknown_race_distance_is_an_error_naming_the_valid_keys(
        self, media_settings: Settings
    ) -> None:
        result = await tools.plot_metric(
            kind="race_prediction", date_range="30d", sport="double_ironman"
        )
        assert result["status"] == "error"
        assert "unknown race distance" in result["detail"].lower()
        assert result["chart_id"] is None and result["media_url"] is None

    def test_docstring_matches_the_registry_race_prediction_is_supported(
        self,
    ) -> None:
        """The docstring is the model-facing schema (WA-6 doctrine): it
        must describe what the tool DOES. ``race_prediction`` is a
        SUPPORTED kind since RP-10 wired it to the engine's distance
        path, so the stale "arrives with Feature 8" promise must be gone
        and the kind must be listed among the supported ones."""
        doc = tools.plot_metric.__doc__ or ""
        assert "arrives with Feature 8" not in doc
        supported_section = doc.split("Returns on success")[0]
        assert '"race_prediction"' in supported_section
