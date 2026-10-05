"""sRPE hrTSS comparison report (LOAD-12): the implied TSS-equivalent factor.

Read-only tool that computes what the sRPE TSS-equivalent factor would be
if strength loads were anchored to heart rate: the MEDIAN of
``hrTSS / sRPE_AU`` over sessions that have BOTH a stored owner-entered
RPE and an HR stream.

This number is INFORMATIONAL EVIDENCE, not the configured factor. The
configured factor (``engine_srpe_tss_equivalent_factor``, settings) comes
from the owner-agreed equivalent-effort anchor (1 h at RPE 7 = 420 Foster
AU ≡ 1 h at threshold = 100 TSS, so 100/420 ≈ 0.2381; see
``app.engine.load``). The hrTSS-implied value (≈ 0.024 in practice) was
considered and explicitly rejected: it reproduces heart rate's systematic
undervaluation of strength work. This tool exists to keep that comparison
reviewable — it shows what the hrTSS anchor would have been, and why it
was rejected. It NEVER writes the factor (or anything else): changing the
default is an explicit owner decision in settings (§14).

Read-only: the tool only SELECTs from ``activity``/``activity_stream``.
Sessions with an RPE but no usable HR side (no HR stream, unconfigured or
degenerate HR thresholds, non-positive duration) are EXCLUDED with a
reportable reason, never silently dropped. With no RPE data at all the
report says so — zero sessions, no invented number, exit code 0.

Method: for every qualifying session compute

    sRPE_AU       = RPE * duration_min          (Foster et al. 2001)
    hrTSS         = the engine's HR-based load for the same session
    implied factor = hrTSS / sRPE_AU

and report the MEDIAN implied factor together with the per-session
values, so the owner can re-review the rejected comparison at any time.

Entry points:

- :func:`implied_srpe_factor` — pure median over session records.
- :func:`collect_srpe_hr_sessions` — one async DB pass building the
  session records (reuses the read layer's :func:`average_hr_bpm` so the
  HR-gap semantics cannot drift, and the pure engine's TRIMP/hrTSS so the
  cross-side math is exactly the load the engine would persist).
- :func:`format_report` — human-readable multi-line report.
- :func:`main` — argparse CLI (``python -m app.tools.calibrate_srpe``).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import get_settings
from app.db.daily_load import thresholds_from_settings, trimp_coefficients_from_settings
from app.db.models import ActivityRow, ActivityStreamRow
from app.db.session import create_db_engine, make_session_factory
from app.engine.load import (
    ThresholdBundle,
    TrimpCoefficients,
    hr_ratio,
    hrtss,
    trimp,
    trimp_at_lthr_reference,
)
from app.services.daily_load import average_hr_bpm

__all__ = [
    "CalibrationReport",
    "ExcludedSession",
    "SrpeHrSession",
    "collect_srpe_hr_sessions",
    "format_report",
    "implied_srpe_factor",
    "main",
]

HR_STREAM_TYPE = "hr"


@dataclass(frozen=True, slots=True)
class SrpeHrSession:
    """One calibration data point: a session with BOTH an RPE and HR.

    ``implied_factor`` is ``hr_tss / srpe_au``: the factor that would make
    this session's sRPE load agree with its hrTSS.
    """

    activity_id: int
    sport: str
    rpe: float
    duration_min: float
    hr_avg_bpm: float
    hr_tss: float
    srpe_au: float

    @property
    def implied_factor(self) -> float:
        """hrTSS / sRPE_AU for this session (srpe_au is always positive:
        RPE >= 1 and positive duration are enforced by collect/ingest)."""
        return self.hr_tss / self.srpe_au


@dataclass(frozen=True, slots=True)
class ExcludedSession:
    """One RPE-bearing session that cannot be calibrated, with the reason."""

    activity_id: int
    sport: str
    reason: str


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Outcome of one calibration pass.

    ``sessions`` are the usable data points, ``excluded`` the RPE-bearing
    sessions rejected with a reason. ``implied_factor`` is the median of
    the per-session factors, or ``None`` when there is no session yet —
    never an invented number.
    """

    sessions: tuple[SrpeHrSession, ...] = ()
    excluded: tuple[ExcludedSession, ...] = ()

    @property
    def implied_factor(self) -> float | None:
        """Median implied factor over the usable sessions (None if none)."""
        return implied_srpe_factor(self.sessions)


def implied_srpe_factor(sessions: Sequence[SrpeHrSession]) -> float | None:
    """Median of ``hrTSS / sRPE_AU`` over the given sessions.

    The median (not the mean) is the documented estimator: robust against
    a single bad data point (a glitched HR stream would otherwise drag the
    whole calibration). Empty input returns ``None`` — no sessions means
    no factor, never 0 and never a guess.
    """
    if not sessions:
        return None
    return statistics.median(s.implied_factor for s in sessions)


def _hr_tss_for_session(
    hr_avg_bpm: float,
    duration_s: float,
    thresholds: ThresholdBundle,
    coefficients: TrimpCoefficients,
) -> float:
    """The engine's hrTSS for one session (the exact hr-path math of
    :func:`app.engine.load.select_load_method`, factored out so the two
    sides cannot drift). Raises ``ValueError`` on unusable inputs."""
    lthr = thresholds.lthr_bpm
    hr_max = thresholds.hr_max_bpm
    hr_rest = thresholds.hr_rest_bpm
    if lthr is None or hr_max is None or hr_rest is None:
        raise ValueError(
            "HR thresholds (LTHR, max, rest) not configured — hrTSS side "
            "of the calibration is unavailable"
        )
    if hr_max <= hr_rest:
        raise ValueError(
            f"degenerate HR threshold range: HRmax {hr_max!r} <= HRrest {hr_rest!r}"
        )
    duration_min = duration_s / 60.0
    trimp_value = trimp(
        duration_min, hr_avg_bpm, hr_rest, hr_max, coefficients=coefficients
    )
    reference = trimp_at_lthr_reference(
        hr_rest, hr_max, lthr, duration_min=60.0, coefficients=coefficients
    )
    tss = hrtss(trimp_value, reference)
    # hr_ratio is the method-selection detail; validating it here keeps the
    # parity with the engine's HR step explicit (it cannot fail after the
    # checks above, but the call documents the shared intensity definition).
    hr_ratio(hr_avg_bpm, hr_rest, hr_max)
    return tss


async def collect_srpe_hr_sessions(
    session: AsyncSession,
    *,
    thresholds: ThresholdBundle,
    coefficients: TrimpCoefficients,
) -> CalibrationReport:
    """One read-only DB pass: build the calibration data points.

    Candidates are exactly the activities with a stored owner-entered RPE
    (``activity.rpe`` is not NULL — LOAD-12 INPUT data). Each candidate
    needs an HR stream for the hrTSS side; everything that cannot be
    calibrated is returned in ``excluded`` with a concrete reason. The
    average HR reuses the read layer's :func:`average_hr_bpm` (stream gaps
    excluded from the mean) and the hrTSS reuses the pure engine, so both
    sides are computed exactly as the engine itself would.
    """
    activities = (
        (await session.execute(select(ActivityRow))).scalars().all()
    )
    candidates = [a for a in activities if a.rpe is not None]
    if not candidates:
        return CalibrationReport()

    hr_payloads: dict[int, list[Sequence[float | None]]] = {}
    stream_rows = (
        (
            await session.execute(
                select(ActivityStreamRow).where(
                    ActivityStreamRow.stream_type == HR_STREAM_TYPE,
                    ActivityStreamRow.activity_id.in_([a.id for a in candidates]),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in stream_rows:
        hr_payloads.setdefault(row.activity_id, []).append(row.payload)

    sessions: list[SrpeHrSession] = []
    excluded: list[ExcludedSession] = []
    for activity in candidates:
        assert activity.rpe is not None  # narrowed by the candidate filter
        try:
            if activity.duration_s is None or activity.duration_s <= 0:
                raise ValueError(
                    f"missing or non-positive duration_s ({activity.duration_s!r})"
                )
            payloads = hr_payloads.get(activity.id)
            if not payloads:
                raise ValueError("no HR stream stored — hrTSS side unavailable")
            hr_avg = average_hr_bpm(payloads[0])
            if hr_avg is None:
                raise ValueError(
                    "HR stream has no valid sample (absent, empty or all gaps)"
                )
            hr_tss = _hr_tss_for_session(
                hr_avg, float(activity.duration_s), thresholds, coefficients
            )
            duration_min = activity.duration_s / 60.0
            srpe_au = activity.rpe * duration_min
            if srpe_au <= 0.0:
                raise ValueError(f"non-positive sRPE load ({srpe_au!r} AU)")
        except ValueError as exc:
            excluded.append(
                ExcludedSession(
                    activity_id=activity.id, sport=activity.type, reason=str(exc)
                )
            )
            continue
        sessions.append(
            SrpeHrSession(
                activity_id=activity.id,
                sport=activity.type,
                rpe=activity.rpe,
                duration_min=duration_min,
                hr_avg_bpm=hr_avg,
                hr_tss=hr_tss,
                srpe_au=srpe_au,
            )
        )
    return CalibrationReport(
        sessions=tuple(sessions), excluded=tuple(excluded)
    )


def format_report(report: CalibrationReport) -> str:
    """Human-readable report of one comparison pass (read-only)."""
    lines = [
        "sRPE hrTSS comparison (LOAD-12) — informational evidence, NOT the",
        "configured factor:",
        "  implied factor = median of hrTSS / sRPE_AU over sessions with",
        "  BOTH a stored owner-entered RPE and an HR stream. The configured",
        "  engine_srpe_tss_equivalent_factor comes from the owner-agreed",
        "  equivalent-effort anchor (1 h at RPE 7 = 420 AU ≡ 1 h at",
        "  threshold = 100 TSS, so 100/420 ≈ 0.2381), not from this report.",
    ]
    if not report.sessions:
        lines.append(
            "sessions with both RPE and HR: 0 (zero sessions) — nothing to "
            "compare yet. Enter an RPE on your gym sessions in "
            "Intervals.icu and re-sync; no number is invented without data."
        )
    else:
        factor = report.implied_factor
        assert factor is not None  # sessions is non-empty here
        lines.append(f"sessions with both RPE and HR: {len(report.sessions)}")
        lines.append("per-session values:")
        lines.extend(
            f"  activity {s.activity_id} ({s.sport}): RPE {s.rpe:g} x "
            f"{s.duration_min:g} min = {s.srpe_au:g} AU, hrTSS "
            f"{s.hr_tss:.4f}, implied factor {s.implied_factor:.6f}"
            for s in report.sessions
        )
        lines.append(f"implied factor (median): {factor:.6f}")
        lines.append(
            "This number is the comparison the owner reviewed when choosing "
            "the anchor factor (the hrTSS anchor was rejected because it "
            "reproduces HR's undervaluation of strength work); this tool "
            "never writes the factor."
        )
    if report.excluded:
        lines.append(
            f"excluded RPE sessions ({len(report.excluded)}) — never silent:"
        )
        lines.extend(
            f"  activity {e.activity_id} ({e.sport}): {e.reason}"
            for e in report.excluded
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the calibration CLI argument parser."""
    return argparse.ArgumentParser(
        prog="python -m app.tools.calibrate_srpe",
        description=(
            "Read-only report: the hrTSS-implied sRPE TSS-equivalent factor "
            "(median of hrTSS / sRPE_AU over sessions with both a stored "
            "owner-entered RPE and an HR stream) — informational evidence "
            "compared against the owner-agreed anchor factor (100/420). "
            "With no RPE data it reports zero sessions. Never writes "
            "anything."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 on success (including zero sessions)."""
    build_parser().parse_args(argv)
    settings = get_settings()
    thresholds = thresholds_from_settings(settings)
    coefficients = trimp_coefficients_from_settings(settings)

    async def _run() -> CalibrationReport:
        engine = create_db_engine(settings.database_url)
        try:
            factory: async_sessionmaker[AsyncSession] = make_session_factory(engine)
            async with factory() as session:
                return await collect_srpe_hr_sessions(
                    session, thresholds=thresholds, coefficients=coefficients
                )
        finally:
            await engine.dispose()

    report = asyncio.run(_run())
    print(format_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
