"""CLI: FIT intake — upload to Intervals.icu, archive the raw bytes, analyse
immediately (ODD fit-intake FI-3/FI-4, PROJECT_BRIEF §5.1/§5.2/§5.3/§6).

Usage::

    python -m app.tools.upload_fit <path...> [--zip-batch N]

Each ``path`` is a file or a directory. Files must be ``.fit`` or
``.fit.gz``; a directory is searched RECURSIVELY (any depth — the owner's
recordings each sit in their own folder) and every ``.fit``/``.fit.gz``
found is processed in one run. Explicit paths can be mixed with
directories; the discovery count is reported before processing, and any
other file is skipped with a per-file reason rather than failing the run.

Batching (``--zip-batch N``, default OFF — one request per file)
---------------------------------------------------------------
The owner's export folder holds 893 files; one request per file is slow.
With ``--zip-batch N`` the tool bundles up to N input files into one
in-memory zip (member names kept, so the platform's per-file content hash
still applies) and uploads ONE request per batch
(``activities-001.zip``, ...), reporting per batch: the created count and
the response's activity ids, or the duplicate outcome with the EXISTING
activity id(s) it matched, or the failure WITH the batch's member list and
a retry hint. With a multi-member zip the response cannot attribute ids to
member files, so per-activity archiving (§5.2) is SKIPPED with an explicit
reason in that case — the per-member LOCAL analysis still runs from each
member's own bytes (it needs no id mapping); re-run the paths without
``--zip-batch`` to archive per file. A 1:1 batch (one id, one member) is
unambiguous and archives normally.

Upload response shape (live-verified 2026-10)
---------------------------------------------
A real upload returns **201** when the activity was created and **200**
when the file's bytes are already present — in BOTH cases a JSON OBJECT
(``{"icu_athlete_id":..., "id":..., "activities":[{"id":...}]}``), not the
array the cookbook describes. A 200 duplicate names the EXISTING activity
it matched; the CLI reports that id because the dedup only matches
BYTE-identical files — a duplicate against an activity that arrived from
another source (e.g. Garmin Connect) means the same ride is now in the
account twice. The parsing (``upload_response_activity_ids``) reads the
``activities`` array, falls back to the top-level ``id`` and tolerates a
bare JSON array of objects.

Per processed file the tool reports exactly one of:

- **created** — the upload created activity/ies (201): the activity id(s)
  are reported, the raw bytes are archived through the ING-6 storage
  interface keyed by the FIRST returned activity id (§5.2) with the stored
  path, and the immediate analysis (below) runs on the local bytes;
- **duplicate** — the platform's content-hash dedup found the file already
  present (200): reported as "already present", nothing archived, no
  analysis (never an error, never silent);
- **error** — anything that failed (unreadable path, upload error, storage
  failure, unparsable file, no usable load method) with its reason;
- **skipped** — a discovered or named file that is not ``.fit``/``
  .fit.gz``: not a failure (the run continues), but reported.

The exit code distinguishes success (0) from ANY failure (1); skips are
not failures.

.gz inputs
----------
The upload endpoint accepts a gz of fit/gpx directly, so ``.fit.gz`` bytes
are sent AS-IS with their ``.fit.gz`` filename — the tool never requires
the caller to decompress. For the LOCAL analysis only, gzip bodies
(detected by the same magic-byte rule the client applies when downloading,
``data[:2] == b"\\x1f\\x8b"``) are decompressed in memory and parsed; a
corrupt gzip is reported as an analysis failure. The archive keeps the
UPLOADED bytes (compressed for ``.fit.gz``): §5.2 archives the file as the
owner handed it over — exactly the bytes the source of record received and
content-hash-dedups — and the decompressed FIT is always recoverable from
the gzip.

Provenance rule (§5.3): the file is the owner's own recording. This tool
NEVER talks to Strava's API and never will — it takes local paths and
uploads them to Intervals.icu (the single data path, §5.1). Nothing here
reads from Strava.

What the immediate analysis can and cannot derive from a FIT (FI-4)
-------------------------------------------------------------------
The analysis runs on the local (decompressed) bytes: no network round
trip, no database write; the activity becomes visible to
``daily_load``/intensity/durability on the next sync.

CAN derive:
- the sport, from the FIT ``session``/``sport`` messages
  (``parse_fit_session_info``), mapped onto the engine's Strava-style
  sport vocabulary (``_FIT_SPORT_TO_ENGINE_SPORT``); an unknown or missing
  sport is REPORTED, never guessed into a method bucket;
- the duration, from the session's ``total_timer_time`` (moving time;
  fallback ``total_elapsed_time``, then the time-stream span);
- the per-second power/HR/speed/cadence/altitude/distance streams
  (``parse_fit_streams``) and from them the average HR.

CANNOT derive (documented, not a gap to paper over):
- **RPE**: the owner's CR-10 effort rating is not recorded in FIT activity
  files, so ``rpe`` stays ``None`` and the sRPE method is skipped with its
  named reason (it can only be the fallback for sports with an
  owner-entered RPE elsewhere);
- **wellness** (HRV, resting HR, sleep): those live in Garmin wellness
  files, never in an activity FIT;
- **thresholds**: FTP/LTHR/HRmax/HRrest/CSS are owner configuration (§14)
  and come from settings, like every other engine caller
  (``thresholds_from_settings``/``trimp_coefficients_from_settings``).

The load comes from ``select_load_method`` (fixed order power ->
pace/speed -> HR -> sRPE, every skipped method named) printed with the
``engine_version``. Where an intensity modality is usable for the file's
sport (``select_intensity_modality``), the per-sample source-zone
classification and pause-aware weighting reuse the intensity service's
rule (imported, not duplicated, so it cannot drift — the same documented
coupling as the cross-check's ``_activity_date``) and the seconds are
mapped into the 3-zone model with the settings' cut points
(``intensity_constants_from_settings``); where it is not, the explicit
reason is printed instead of a zero split.
"""

from __future__ import annotations

import argparse
import gzip
import io
import math
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from app.core.settings import Settings, get_settings
from app.db.daily_load import (
    thresholds_from_settings,
    trimp_coefficients_from_settings,
)
from app.db.engine_readiness_config import intensity_constants_from_settings
from app.engine.intensity import (
    IntensityModalityKey,
    SportKey,
    has_usable_samples,
    map_to_three_zones,
    select_intensity_modality,
    three_zone_model,
)
from app.engine.load import (
    ActivityLoadInput,
    ThresholdBundle,
    TrimpCoefficients,
    select_load_method,
)
from app.ingest.client import IntervalsClient
from app.ingest.exceptions import IntervalsHTTPError
from app.ingest.fit_parser import (
    FitSessionInfo,
    parse_fit_session_info,
    parse_fit_streams,
)
from app.ingest.models import ActivityUploadResult
from app.ingest.storage import RawFileStorage, storage_from_settings
from app.services.intensity import _classify_samples, _weighted_zone_seconds

__all__ = [
    "MAX_ZIP_BATCH",
    "AnalysisReport",
    "BatchOutcome",
    "FileOutcome",
    "UploadClient",
    "analyse_fit_bytes",
    "analyse_fit_streams",
    "build_parser",
    "format_batch_outcome",
    "format_outcome",
    "main",
    "process_batch",
]

_GZIP_MAGIC: Final = b"\x1f\x8b"
"""Gzip magic bytes — the SAME detection rule the client applies to
downloaded bodies (``IntervalsClient._gunzip``); kept as a local constant
because the tool must not depend on client internals."""

MAX_ZIP_BATCH: Final = 1000
"""Upper bound for ``--zip-batch`` (owner-reviewable): the cookbook sets no
batch-size limit, so the bound only guards against an absurd single request
(a few hundred ~100 kB .fit.gz files is already a very large body)."""

BATCH_RETRY_HINT: Final = (
    "re-run these paths without --zip-batch to upload (and archive and "
    "attribute) file by file"
)
"""The retry hint printed whenever a batch loses per-file attribution."""

_FIT_SUFFIXES: Final = (".fit", ".fit.gz")
"""File types accepted as intake input (the endpoint also accepts gpx,
tcx, zips and gpx.gz; the intake is a FIT-feature and reports other
extensions as skips rather than guessing)."""

# FIT sport enum names (fitdecode's decoded values) -> the engine's
# Strava-style sport vocabulary (app.engine.load._KNOWN_SPORTS). Names
# outside this table are REPORTED as unmappable — never guessed.
_FIT_SPORT_TO_ENGINE_SPORT: Final[dict[str, str]] = {
    "cycling": "ride",
    "indoor_cycling": "virtualride",
    "e_biking": "ebikeride",
    "running": "run",
    "treadmill": "treadmillrun",
    "trail": "trailrun",
    "swimming": "swim",
    "lap_swimming": "swim",
    "open_water": "swim",
    "walking": "walk",
    "hiking": "hike",
    "training": "workout",  # FIT 'training' = strength-training session
}

# Engine sport -> the 3-zone intensity sport (run/bike/swim); walk, hike
# and workout have NO intensity modality and report the reason instead.
_ENGINE_SPORT_TO_INTENSITY_SPORT: Final[dict[str, SportKey]] = {
    "ride": "bike",
    "virtualride": "bike",
    "ebikeride": "bike",
    "run": "run",
    "treadmillrun": "run",
    "trailrun": "run",
    "swim": "swim",
}


class UploadClient(Protocol):
    """The seam the CLI needs from :class:`IntervalsClient` (FI-1/2)."""

    def upload_activity_file(
        self,
        data: bytes,
        *,
        filename: str,
        name: str | None = None,
        description: str | None = None,
    ) -> ActivityUploadResult:
        ...


@dataclass(frozen=True)
class AnalysisReport:
    """The immediate analysis of one created FIT file (FI-4).

    ``sport`` is the engine sport the analysis ran on, ``method``/``tss``
    the chosen load method and value (``engine_version`` stamped), and the
    ``z*_seconds``/``percentages`` the 3-zone split — all ``None`` (with
    ``zone_reason`` set) when no intensity modality is usable for the
    sport: an explicit reason instead of a fabricated zero split. ``rpe``
    is always ``None``: FIT activity files do not carry an owner RPE.
    """

    sport: str
    duration_s: float
    method: str
    tss: float
    engine_version: str
    modality: str | None = None
    z1_seconds: float | None = None
    z2_seconds: float | None = None
    z3_seconds: float | None = None
    percentages: tuple[float, float, float] | None = None
    zone_reason: str | None = None
    rpe: float | None = None


@dataclass(frozen=True)
class FileOutcome:
    """One processed (or skipped) input path, human- and test-facing."""

    path: str
    status: str  # "created" | "duplicate" | "error" | "skipped"
    activity_ids: tuple[str, ...] = ()
    stored_path: str | None = None
    reason: str | None = None
    analysis: AnalysisReport | None = None


@dataclass(frozen=True)
class BatchOutcome:
    """One processed ``--zip-batch`` zip upload, human- and test-facing.

    ``status`` is "created" (the zip created activity/ies), "duplicate"
    (everything already present) or "error" (the batch upload failed).
    ``activity_ids`` carries the ids the response exposed; ``archived``
    lists ``(member path, stored path)`` pairs — ONLY populated when the
    id->member mapping is unambiguous (a single id for a single member;
    a multi-member zip response exposes ids in an order we cannot map to
    member files, so archiving is skipped with ``archive_reason`` and the
    retry hint instead of guessing). ``analyses`` holds the per-member
    local analysis (which never needs the id mapping — it runs from each
    member's own bytes); ``analysis_failures`` the members whose analysis
    failed with their reasons. ``members_read`` are the member paths that
    made it into the zip; ``unreadable`` the members that could not be
    read (each reported as an error).
    """

    batch_name: str
    member_paths: tuple[str, ...]
    status: str  # "created" | "duplicate" | "error"
    activity_ids: tuple[str, ...] = ()
    archived: tuple[tuple[str, str], ...] = ()
    archive_reason: str | None = None
    analyses: tuple[tuple[str, AnalysisReport], ...] = ()
    analysis_failures: tuple[tuple[str, str], ...] = ()
    members_read: tuple[str, ...] = ()
    unreadable: tuple[tuple[str, str], ...] = ()
    reason: str | None = None


def decompress_if_gzip(data: bytes) -> bytes:
    """Decompress a gzip body (magic bytes), passing other bytes through.

    The same rule the client applies to downloaded bodies; a corrupt gzip
    raises ``ValueError`` with the reason (the caller reports it).
    """
    if data[:2] != _GZIP_MAGIC:
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise ValueError(f"cannot decompress gzip data: {exc}") from exc


def _threshold_for(
    modality: IntensityModalityKey, thresholds: ThresholdBundle
) -> float | None:
    """The configured threshold the modality's classification needs."""
    if modality == "bike_power":
        return thresholds.ftp_watts
    if modality in ("run_hr", "bike_hr"):
        return thresholds.lthr_bpm
    return thresholds.css_mps


def _threshold_label(modality: IntensityModalityKey) -> str:
    if modality == "bike_power":
        return "FTP"
    if modality in ("run_hr", "bike_hr"):
        return "LTHR"
    return "CSS"


def analyse_fit_streams(
    info: FitSessionInfo,
    streams: dict[str, list[float | None]],
    *,
    engine_version: str,
    thresholds: ThresholdBundle,
    coefficients: TrimpCoefficients,
    np_window_samples: int,
    np_min_valid_fraction: float,
    trimp_reference_minutes: float,
    first_threshold_pcts: dict[IntensityModalityKey, float],
    second_threshold_pcts: dict[IntensityModalityKey, float],
    speed_tolerance_mps: float,
    gap_cap_median_multiple: float,
) -> AnalysisReport:
    """Build the engine's load and 3-zone split from already-parsed FIT data.

    Raises ``ValueError`` with a clear reason when the sport cannot be
    determined, the duration cannot be derived, or no load method applies
    (the engine's own all-skips error). Everything else degrades to
    explicit reasons on the report (``zone_reason``), never to zeros.
    """
    fit_sport = info.sport
    engine_sport = (
        _FIT_SPORT_TO_ENGINE_SPORT.get(fit_sport) if fit_sport is not None else None
    )
    if engine_sport is None:
        raise ValueError(
            f"cannot determine the sport from the FIT file (sport "
            f"{fit_sport!r} is missing or unmapped); no analysis without "
            "the sport — never guessed"
        )

    duration_s = info.total_timer_time_s or info.total_elapsed_time_s
    if duration_s is None:
        times = [t for t in streams.get("time", []) if t is not None]
        if len(times) >= 2 and times[-1] > times[0]:
            duration_s = times[-1] - times[0]
    if duration_s is None:
        raise ValueError(
            "cannot determine the session duration (no session "
            "total_timer_time/total_elapsed_time and no usable time stream)"
        )

    hr_valid = [h for h in streams.get("hr", []) if h is not None]
    hr_avg = math.fsum(hr_valid) / len(hr_valid) if hr_valid else None

    # FIT activity files carry no owner RPE (documented in the module
    # docstring): rpe stays None, sRPE reports its skip reason if reached.
    rpe: float | None = None
    activity = ActivityLoadInput(
        sport=engine_sport,
        duration_s=duration_s,
        power_samples=streams.get("power"),
        speed_samples=streams.get("speed"),
        distance_samples=streams.get("distance"),
        altitude_samples=streams.get("altitude"),
        hr_avg_bpm=hr_avg,
        rpe=rpe,
    )
    selection = select_load_method(
        activity,
        thresholds,
        coefficients=coefficients,
        np_window_samples=np_window_samples,
        np_min_valid_fraction=np_min_valid_fraction,
        trimp_reference_minutes=trimp_reference_minutes,
    )

    modality: str | None = None
    z1: float | None = None
    z2: float | None = None
    z3: float | None = None
    percentages: tuple[float, float, float] | None = None
    zone_reason: str | None = None

    intensity_sport = _ENGINE_SPORT_TO_INTENSITY_SPORT.get(engine_sport)
    if intensity_sport is None:
        zone_reason = (
            f"no intensity modality for sport {engine_sport!r}: not one of "
            "run/bike/swim (walk/strength sessions have no zone table)"
        )
    else:
        try:
            selected = select_intensity_modality(
                intensity_sport,
                has_power_samples=has_usable_samples(streams.get("power")),
                has_hr_samples=has_usable_samples(streams.get("hr")),
            )
        except ValueError as exc:
            zone_reason = str(exc)
        else:
            modality = selected
            threshold = _threshold_for(selected, thresholds)
            if threshold is None:
                zone_reason = (
                    f"{_threshold_label(selected)} not configured; the "
                    f"{selected} zone classification cannot run — never "
                    "guessed"
                )
            else:
                classified = _classify_samples(
                    selected,
                    streams["hr"] if selected in ("run_hr", "bike_hr")
                    else streams["power"] if selected == "bike_power"
                    else streams["speed"],
                    ftp_watts=thresholds.ftp_watts,
                    lthr_bpm=thresholds.lthr_bpm,
                    css_mps=thresholds.css_mps,
                )
                zone_seconds = _weighted_zone_seconds(
                    classified,
                    streams.get("time"),
                    streams.get("speed"),
                    speed_tolerance_mps=speed_tolerance_mps,
                    gap_cap_median_multiple=gap_cap_median_multiple,
                )
                mapped = map_to_three_zones(
                    three_zone_model(
                        selected,
                        first_threshold_pct=first_threshold_pcts[selected],
                        second_threshold_pct=second_threshold_pcts[selected],
                    ),
                    zone_seconds,
                )
                if mapped.total_seconds > 0.0:
                    z1 = mapped.z1_seconds
                    z2 = mapped.z2_seconds
                    z3 = mapped.z3_seconds
                    percentages = (
                        z1 / mapped.total_seconds * 100.0,
                        z2 / mapped.total_seconds * 100.0,
                        z3 / mapped.total_seconds * 100.0,
                    )
                else:
                    zone_reason = (
                        "no time could be classified in zone (all samples "
                        "are gaps or non-moving)"
                    )

    return AnalysisReport(
        sport=engine_sport,
        duration_s=duration_s,
        method=selection.method,
        tss=selection.tss,
        engine_version=engine_version,
        modality=modality,
        z1_seconds=z1,
        z2_seconds=z2,
        z3_seconds=z3,
        percentages=percentages,
        zone_reason=zone_reason,
        rpe=rpe,
    )


def analyse_fit_bytes(data: bytes, **kwargs: Any) -> AnalysisReport:
    """Analyse one file's raw bytes: decompress (gzip) in memory, parse the
    FIT, then run :func:`analyse_fit_streams`. Raises ``ValueError`` with a
    clear reason for a non-FIT file or a corrupt gzip body.
    """
    fit_bytes = decompress_if_gzip(data)
    info = parse_fit_session_info(fit_bytes)
    streams = parse_fit_streams(fit_bytes)
    return analyse_fit_streams(info, streams, **kwargs)


def _engine_kwargs(config: Settings) -> dict[str, Any]:
    """Engine parameters sourced from settings (§14), never re-hardcoded."""
    intensity = intensity_constants_from_settings(config)
    return {
        "engine_version": config.engine_version,
        "thresholds": thresholds_from_settings(config),
        "coefficients": trimp_coefficients_from_settings(config),
        "np_window_samples": config.engine_np_window_samples,
        "np_min_valid_fraction": config.engine_min_valid_fraction,
        "trimp_reference_minutes": config.engine_trimp_reference_minutes,
        "first_threshold_pcts": intensity["first_threshold_pcts"],
        "second_threshold_pcts": intensity["second_threshold_pcts"],
        "speed_tolerance_mps": intensity["speed_tolerance_mps"],
        "gap_cap_median_multiple": intensity["gap_cap_median_multiple"],
    }


def _is_fit_file(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".fit") or name.endswith(".fit.gz")


def discover_paths(raw_paths: Sequence[str]) -> tuple[list[Path], list[tuple[str, str]]]:
    """Resolve CLI paths into (work items, skips).

    A directory is searched recursively for ``.fit``/``.fit.gz`` files
    (sorted for deterministic output); any other file inside it is a skip
    with a reason. An explicit existing non-FIT file is likewise a skip.
    A NON-EXISTENT path stays a work item so the per-file processing
    reports it as an unreadable error (exit 1), not a silent skip.
    """
    work: list[Path] = []
    skips: list[tuple[str, str]] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if not candidate.is_file():
                    continue
                if _is_fit_file(candidate):
                    work.append(candidate)
                else:
                    skips.append(
                        (
                            str(candidate),
                            f"not a .fit or .fit.gz file ({candidate.name!r})",
                        )
                    )
        elif path.is_file():
            if _is_fit_file(path):
                work.append(path)
            else:
                skips.append(
                    (str(path), f"not a .fit or .fit.gz file ({path.name!r})")
                )
        else:
            work.append(path)  # unreadable: reported as an error per file
    return work, skips


def process_file(
    path: Path,
    *,
    client: UploadClient,
    storage: RawFileStorage,
    engine_kwargs: dict[str, Any],
) -> FileOutcome:
    """Upload, archive and analyse one file; never raise (report instead)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return FileOutcome(path=str(path), status="error", reason=f"unreadable: {exc}")
    try:
        result = client.upload_activity_file(data, filename=path.name)
    except IntervalsHTTPError as exc:
        return FileOutcome(
            path=str(path), status="error", reason=f"upload failed: {exc}"
        )
    if not result.created:
        # A 200 duplicate names the EXISTING activity it matched
        # (live-verified shape). The distinction matters to the owner: a
        # duplicate against an activity that arrived from ANOTHER source
        # (e.g. Garmin Connect) means the same ride is now in the account
        # twice (the dedup only matches byte-identical files).
        matched = (
            ", ".join(result.activity_ids) if result.activity_ids else "no id returned"
        )
        return FileOutcome(
            path=str(path),
            status="duplicate",
            activity_ids=result.activity_ids,
            reason=f"already present as activity {matched}; nothing archived, "
            "no analysis",
        )
    if not result.activity_ids:
        return FileOutcome(
            path=str(path),
            status="error",
            reason="upload created an activity but returned no activity id; "
            "cannot archive or analyse",
        )
    activity_id = result.activity_ids[0]
    # Archive the UPLOADED bytes (compressed for .fit.gz — see the module
    # docstring for why), keyed by the returned activity id (§5.2).
    try:
        stored = storage.save(activity_id, data)
    except (OSError, ValueError) as exc:
        return FileOutcome(
            path=str(path),
            status="error",
            activity_ids=(activity_id,),
            reason=f"storage failed: {exc}",
        )
    try:
        analysis = analyse_fit_bytes(data, **engine_kwargs)
    except ValueError as exc:
        return FileOutcome(
            path=str(path),
            status="error",
            activity_ids=(activity_id,),
            stored_path=stored,
            reason=f"analysis failed: {exc}",
        )
    return FileOutcome(
        path=str(path),
        status="created",
        activity_ids=(activity_id,),
        stored_path=stored,
        analysis=analysis,
    )


def _batch_size(value: str) -> int:
    """argparse type for ``--zip-batch``: an integer in 1..MAX_ZIP_BATCH."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if not 1 <= parsed <= MAX_ZIP_BATCH:
        raise argparse.ArgumentTypeError(
            f"--zip-batch must be between 1 and {MAX_ZIP_BATCH}, got {parsed}"
        )
    return parsed


def _unique_arcname(name: str, used: set[str]) -> str:
    """The member name inside the batch zip: the original name, deduped
    only on a same-name collision within ONE batch (the platform's
    content-hash dedup needs distinct member entries)."""
    if name not in used:
        return name
    stem, dot, ext = name.rpartition(".")
    index = 2
    while True:
        candidate = f"{stem}-{index}{dot}{ext}" if dot else f"{name}-{index}"
        if candidate not in used:
            return candidate
        index += 1


def build_batch_zip(members: Sequence[tuple[str, bytes]]) -> bytes:
    """One in-memory zip of ``(member_name, bytes)`` pairs, keeping the
    original member names so the platform's per-file content hash still
    applies (the endpoint accepts a zip of fit/gpx/gz files)."""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            arcname = _unique_arcname(name, used)
            used.add(arcname)
            zf.writestr(arcname, data)
    return buffer.getvalue()


def process_batch(
    members: Sequence[Path],
    batch_name: str,
    *,
    client: UploadClient,
    storage: RawFileStorage,
    engine_kwargs: dict[str, Any],
) -> BatchOutcome:
    """Upload up to N files as ONE zip and report the batch outcome.

    - created: the response's ids are reported; per-activity archiving
      happens ONLY when the id->member mapping is unambiguous (a single id
      for a single member) — with several ids the response exposes no
      member attribution, so archiving is SKIPPED with an explicit reason
      and the retry hint (never guessed). The per-member LOCAL analysis
      always runs from each member's own bytes (it needs no id mapping).
    - duplicate: the response carries the EXISTING activity id(s) it
      matched; reported with the member list, no analysis, no archive.
    - error: reported with the batch's member list and the retry hint so
      no file loses track.
    """
    unreadable: list[tuple[str, str]] = []
    read: list[tuple[str, str, bytes]] = []  # (path, name, bytes)
    for member in members:
        try:
            data = member.read_bytes()
        except OSError as exc:
            unreadable.append((str(member), f"unreadable: {exc}"))
            continue
        read.append((str(member), member.name, data))
    if not read:
        return BatchOutcome(
            batch_name=batch_name,
            member_paths=tuple(str(m) for m in members),
            status="error",
            unreadable=tuple(unreadable),
            reason="no member of the batch could be read; nothing uploaded",
        )
    zipped = build_batch_zip([(name, data) for _, name, data in read])
    try:
        result = client.upload_activity_file(zipped, filename=batch_name)
    except IntervalsHTTPError as exc:
        return BatchOutcome(
            batch_name=batch_name,
            member_paths=tuple(str(m) for m in members),
            status="error",
            members_read=tuple(path for path, _, _ in read),
            unreadable=tuple(unreadable),
            reason=f"upload failed: {exc} | {BATCH_RETRY_HINT}",
        )
    if not result.created:
        return BatchOutcome(
            batch_name=batch_name,
            member_paths=tuple(str(m) for m in members),
            status="duplicate",
            activity_ids=result.activity_ids,
            members_read=tuple(path for path, _, _ in read),
            unreadable=tuple(unreadable),
            reason=(
                f"all {len(read)} member(s) already present (matched ids: "
                f"{', '.join(result.activity_ids) if result.activity_ids else 'none exposed'}); "
                "nothing archived, no analysis"
            ),
        )
    ids = result.activity_ids
    if not ids:
        return BatchOutcome(
            batch_name=batch_name,
            member_paths=tuple(str(m) for m in members),
            status="error",
            members_read=tuple(path for path, _, _ in read),
            unreadable=tuple(unreadable),
            reason="upload created activity/ies but the response exposed no "
            f"activity id | {BATCH_RETRY_HINT}",
        )
    # Per-activity archiving ONLY when unambiguous: one id for one member.
    archived: list[tuple[str, str]] = []
    archive_reason: str | None = None
    if len(ids) == 1 and len(read) == 1:
        member_path, _, member_bytes = read[0]
        try:
            stored = storage.save(ids[0], member_bytes)
        except (OSError, ValueError) as exc:
            return BatchOutcome(
                batch_name=batch_name,
                member_paths=tuple(str(m) for m in members),
                status="error",
                activity_ids=ids,
                members_read=tuple(path for path, _, _ in read),
                unreadable=tuple(unreadable),
                reason=f"storage failed for activity {ids[0]} ({member_path}): {exc}",
            )
        archived.append((member_path, stored))
    else:
        archive_reason = (
            f"skipped: cannot map {len(ids)} created activity id(s) to "
            f"{len(read)} batch member(s) unambiguously — the bytes are on "
            f"Intervals.icu; {BATCH_RETRY_HINT}"
        )
    analyses: list[tuple[str, AnalysisReport]] = []
    failures: list[tuple[str, str]] = []
    for member_path, _, member_bytes in read:
        try:
            analyses.append(
                (member_path, analyse_fit_bytes(member_bytes, **engine_kwargs))
            )
        except ValueError as exc:
            failures.append((member_path, f"analysis failed: {exc}"))
    return BatchOutcome(
        batch_name=batch_name,
        member_paths=tuple(str(m) for m in members),
        status="created",
        activity_ids=ids,
        archived=tuple(archived),
        archive_reason=archive_reason,
        analyses=tuple(analyses),
        analysis_failures=tuple(failures),
        members_read=tuple(path for path, _, _ in read),
        unreadable=tuple(unreadable),
    )


def format_batch_outcome(outcome: BatchOutcome) -> str:
    """One batch's human-readable report lines (bounded output)."""
    member_count = len(outcome.members_read)
    member_list = ", ".join(Path(p).name for p in outcome.members_read)
    lines: list[str] = []
    if outcome.status == "duplicate":
        assert outcome.reason is not None
        lines.append(
            f"{outcome.batch_name} (batch, {member_count} file(s)): duplicate — "
            f"{outcome.reason} | members: {member_list}"
        )
    elif outcome.status == "error":
        assert outcome.reason is not None
        lines.append(
            f"{outcome.batch_name} (batch, {member_count} file(s)): error: "
            f"{outcome.reason} | members: {member_list}"
        )
    else:
        ids = ", ".join(outcome.activity_ids)
        lines.append(
            f"{outcome.batch_name} (batch, {member_count} file(s)): created "
            f"{len(outcome.activity_ids)} activity id(s): {ids} | members: "
            f"{member_list}"
        )
        if outcome.archived:
            lines.extend(
                f"  archive: {stored} (activity {Path(member).name})"
                for member, stored in outcome.archived
            )
        if outcome.archive_reason is not None:
            lines.append(f"  archive: {outcome.archive_reason}")
        for member, report in outcome.analyses:
            lines.append(f"  analysis {Path(member).name}:")
            lines.append(
                f"    load: method={report.method} tss={report.tss:.2f} "
                f"engine_version={report.engine_version}"
            )
            if (
                report.modality is not None
                and report.z1_seconds is not None
                and report.z2_seconds is not None
                and report.z3_seconds is not None
                and report.percentages is not None
            ):
                p1, p2, p3 = report.percentages
                lines.append(
                    f"    zones: modality={report.modality} "
                    f"Z1={report.z1_seconds:.2f}s ({p1:.2f}%) "
                    f"Z2={report.z2_seconds:.2f}s ({p2:.2f}%) "
                    f"Z3={report.z3_seconds:.2f}s ({p3:.2f}%)"
                )
            else:
                lines.append(f"    zones: not computed: {report.zone_reason}")
        for member, failure in outcome.analysis_failures:
            lines.append(f"  {Path(member).name}: {failure}")
    lines.extend(
        f"  {Path(member).name}: error: {reason}"
        for member, reason in outcome.unreadable
    )
    return "\n".join(lines)


def format_outcome(outcome: FileOutcome) -> str:
    """One file's human-readable report lines (bounded output)."""
    if outcome.status == "duplicate":
        assert outcome.reason is not None
        lines = [f"{outcome.path}: duplicate ({outcome.reason})"]
        if outcome.activity_ids:
            lines.append(
                "  note: dedup matches byte-identical files only — a duplicate "
                "against an activity from another source (e.g. Garmin Connect) "
                "means the ride may be in the account twice"
            )
        return "\n".join(lines)
    if outcome.status == "error":
        line = f"{outcome.path}: error: {outcome.reason}"
        if outcome.activity_ids or outcome.stored_path:
            # The upload DID succeed before this failure — report it too
            # (never silent about what already happened).
            prefix = (
                f"{outcome.path}: created activity {', '.join(outcome.activity_ids)}"
            )
            if outcome.stored_path is not None:
                prefix += f" | archived {outcome.stored_path}"
            line = f"{prefix}; error: {outcome.reason}"
        return line
    if outcome.status == "skipped":
        return f"{outcome.path}: skipped: {outcome.reason}"
    # created: the upload result plus, when available, archive and analysis.
    ids = ", ".join(outcome.activity_ids)
    line = f"{outcome.path}: created activity {ids}"
    if outcome.stored_path is not None:
        line += f" | archived {outcome.stored_path}"
    lines = [line]
    analysis = outcome.analysis
    if analysis is not None:
        lines.append(
            f"  load: method={analysis.method} tss={analysis.tss:.2f} "
            f"engine_version={analysis.engine_version}"
        )
        if (
            analysis.modality is not None
            and analysis.z1_seconds is not None
            and analysis.z2_seconds is not None
            and analysis.z3_seconds is not None
            and analysis.percentages is not None
        ):
            p1, p2, p3 = analysis.percentages
            lines.append(
                f"  zones: modality={analysis.modality} "
                f"Z1={analysis.z1_seconds:.2f}s ({p1:.2f}%) "
                f"Z2={analysis.z2_seconds:.2f}s ({p2:.2f}%) "
                f"Z3={analysis.z3_seconds:.2f}s ({p3:.2f}%)"
            )
        else:
            lines.append(f"  zones: not computed: {analysis.zone_reason}")
    elif outcome.reason is not None:
        line += f"; {outcome.reason}"
        lines = [line]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the intake CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.upload_fit",
        description=(
            "Upload .fit/.fit.gz files to Intervals.icu (the single data "
            "path, §5.1; never Strava, §5.3), archive the raw bytes locally "
            "keyed by the returned activity id (§5.2) and analyse each "
            "CREATED file immediately from the local bytes (load method, "
            "TSS, 3-zone split, engine_version). Directories are searched "
            "recursively; duplicates, skips and failures are all reported "
            "with reasons."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="a .fit/.fit.gz file, or a directory to search recursively",
    )
    parser.add_argument(
        "--zip-batch",
        type=_batch_size,
        default=None,
        metavar="N",
        help=(
            f"bundle up to N input files into one in-memory zip and upload "
            f"ONE request per batch (activities-001.zip, ...). Default: off "
            f"(one request per file). Bounds 1..{MAX_ZIP_BATCH}. Note: with "
            "a multi-file batch the response cannot attribute activity ids "
            "to member files, so per-activity archiving is skipped with an "
            "explicit reason (the per-file local analysis still runs); "
            "re-run without --zip-batch to archive per file."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: UploadClient | None = None,
    storage: RawFileStorage | None = None,
    settings: Settings | None = None,
) -> int:
    """CLI entry point: 0 when every processed file succeeded, 1 on any
    failure (skips are not failures). ``client``/``storage`` are injectable
    seams so tests run without network or disk side effects.
    """
    args = build_parser().parse_args(argv)
    config = settings or get_settings()
    upload_client = client if client is not None else IntervalsClient(config)
    file_storage = storage if storage is not None else storage_from_settings(config)

    work, skips = discover_paths(args.paths)
    for skip_path, reason in skips:
        print(
            format_outcome(
                FileOutcome(path=skip_path, status="skipped", reason=reason)
            )
        )
    print(f"discovered {len(work)} file(s) to process")

    engine_kwargs = _engine_kwargs(config)
    failed = False
    batch_size = args.zip_batch
    if batch_size is None:
        for path in work:
            outcome = process_file(
                path,
                client=upload_client,
                storage=file_storage,
                engine_kwargs=engine_kwargs,
            )
            print(format_outcome(outcome))
            if outcome.status == "error":
                failed = True
    else:
        # Batched mode: bundle up to N files per in-memory zip, ONE request
        # per batch (activities-001.zip, ...). A batch that fails keeps its
        # member list in the report; a created multi-member batch cannot
        # attribute ids to members, so per-activity archiving is skipped
        # with an explicit reason while the per-member LOCAL analysis still
        # runs (it needs no id mapping).
        for index, start in enumerate(range(0, len(work), batch_size), start=1):
            batch = work[start : start + batch_size]
            batch_outcome = process_batch(
                batch,
                f"activities-{index:03d}.zip",
                client=upload_client,
                storage=file_storage,
                engine_kwargs=engine_kwargs,
            )
            print(format_batch_outcome(batch_outcome))
            if batch_outcome.status == "error" or batch_outcome.analysis_failures:
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
