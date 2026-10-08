"""Tests for the FIT intake CLI (ODD tasks FI-3/FI-4).

``python -m app.tools.upload_fit <path...>``:

- uploads each file to Intervals.icu (FI-1/2 client), reporting per file
  whether it was created (with the activity id) or a duplicate (already
  present — never an error, never silent) or failed (with a reason);
- archives the raw bytes of CREATED files through the ING-6 storage
  interface, keyed by the returned activity id (§5.2);
- analyses each created file immediately from the LOCAL bytes (FI-4): FIT
  parse -> ``ActivityLoadInput`` -> method selection -> load + 3-zone split,
  printed with ``engine_version``. No network round trip, no DB write.

Every test injects the client/storage seams (no network, no disk side
effects); the analysis expectations are ENGINE-PRODUCED: built in the test
from the same pure engine functions the CLI calls, plus hand-derived
values (fixture duration 60.363 s; the documented interval rule makes the
zone-seconds total 62.0 s = span 61 + last sample inheriting the previous
interval).
"""

from __future__ import annotations

import gzip
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.core.settings import Settings
from app.db.daily_load import (
    thresholds_from_settings,
    trimp_coefficients_from_settings,
)
from app.db.engine_readiness_config import intensity_constants_from_settings
from app.engine.intensity import (
    IntensityModalityKey,
    map_to_three_zones,
    three_zone_model,
)
from app.engine.load import ActivityLoadInput, select_load_method
from app.ingest.exceptions import IntervalsClientError
from app.ingest.fit_parser import parse_fit_streams
from app.ingest.models import ActivityUploadResult
from app.services.intensity import _classify_samples, _weighted_zone_seconds
from app.tools.upload_fit import analyse_fit_streams, build_parser, main

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "garmin-fenix-5-bike.fit"
)
FIT_BYTES = FIXTURE_PATH.read_bytes()
"""Real fixture FIT bytes: a "created" outcome needs a parseable FIT for
the immediate analysis (FI-4), so the default test file is the fixture."""


class StubUploadClient:
    """UploadClient seam: records calls, returns fixed result(s)/exception.

    ``results`` may be a single value (returned for every call) or a list
    (popped per call) so multi-file runs can mix created/duplicate/error.
    """

    def __init__(
        self,
        results: ActivityUploadResult | Exception | list[ActivityUploadResult | Exception],
    ) -> None:
        self._results = results
        self.calls: list[tuple[bytes, str]] = []

    def upload_activity_file(
        self,
        data: bytes,
        *,
        filename: str,
        name: str | None = None,
        description: str | None = None,
    ) -> ActivityUploadResult:
        self.calls.append((data, filename))
        result = (
            self._results.pop(0) if isinstance(self._results, list) else self._results
        )
        if isinstance(result, Exception):
            raise result
        return result


class StubStorage:
    """RawFileStorage seam: records saves without touching the disk."""

    def __init__(self, *, fail: bool = False) -> None:
        self.saved: list[tuple[str, bytes]] = []
        self._fail = fail

    def save(self, activity_id: str, fit_bytes: bytes) -> str:
        if self._fail:
            raise OSError("disk full")
        self.saved.append((activity_id, fit_bytes))
        return f"{activity_id}.fit"

    def load(self, path: str) -> bytes:
        raise AssertionError("not used by the CLI")

    def exists(self, path: str) -> bool:
        raise AssertionError("not used by the CLI")


def make_settings(**overrides: Any) -> Settings:
    """Explicit thresholds so the engine numbers are deterministic.

    The fixture is a HR-only bike ride (no power samples), so the load
    method is 'hr' and the intensity modality 'bike_hr'.
    """
    return Settings(
        athlete_ftp_w=250.0,
        athlete_lthr_bpm=152.0,
        athlete_hr_max_bpm=190.0,
        athlete_hr_rest_bpm=55.0,
        athlete_threshold_run_speed_mps=None,
        athlete_css_speed_mps=None,
        **overrides,
    )


def write_tmp_fit(tmp_path: Path, data: bytes, name: str = "ride.fit") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# FI-3: upload, archive, report (created / duplicate / error)
# ---------------------------------------------------------------------------


class TestUploadAndArchive:
    def test_created_reports_activity_id_and_archived_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(tmp_path, FIT_BYTES)
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i42",))
        )
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "created" in out
        assert "i42" in out
        assert "i42.fit" in out  # the archived storage-relative path
        # The raw bytes are archived keyed by the RETURNED activity id (§5.2).
        assert storage.saved == [("i42", FIT_BYTES)]
        # The upload carries the local file's name as its filename.
        assert client.calls == [(FIT_BYTES, "ride.fit")]

    def test_duplicate_is_reported_not_archived_not_analysed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(tmp_path, FIT_BYTES, name="dup.fit")
        # Observed shape: a duplicate names the EXISTING activity it matched
        # (here a Garmin-sourced one — the ride may be in the account twice).
        client = StubUploadClient(
            ActivityUploadResult(created=False, activity_ids=("i140561801",))
        )
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "duplicate" in out
        assert "already present as activity i140561801" in out
        # The owner must see the double-presence risk explicitly.
        assert "Garmin Connect" in out
        assert storage.saved == []  # nothing archived
        assert "load:" not in out  # no analysis
        assert "zones:" not in out

    def test_unreadable_path_is_an_error_with_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i1",))
        )
        storage = StubStorage()
        exit_code = main(
            [str(tmp_path / "does-not-exist.fit")],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "error" in out
        assert "unreadable" in out
        assert client.calls == []  # never uploaded

    def test_upload_error_is_reported_with_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(tmp_path, FIT_BYTES, name="bad.fit")
        client = StubUploadClient(IntervalsClientError(400, "invalid file"))
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "error" in out
        assert "upload failed" in out
        assert "invalid file" in out

    def test_storage_failure_is_reported_with_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(tmp_path, FIT_BYTES, name="ok.fit")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i9",))
        )
        storage = StubStorage(fail=True)
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "error" in out
        assert "storage" in out
        assert "disk full" in out

    def test_mixed_files_each_reported_and_exit_code_is_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good = write_tmp_fit(tmp_path, FIT_BYTES, name="good.fit")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i5",))
        )
        storage = StubStorage()
        exit_code = main(
            [good, str(tmp_path / "missing.fit")],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "good.fit" in out and "created" in out
        assert "missing.fit" in out and "error" in out

    def test_parser_requires_at_least_one_path(self) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args([])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Scope addition 1: .fit.gz inputs (upload compressed, analyse decompressed)
# ---------------------------------------------------------------------------


class TestGzipInput:
    def test_fit_gz_uploads_compressed_bytes_and_analyses_decompressed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        compressed = gz(fit_bytes)
        path = write_tmp_fit(tmp_path, compressed, name="ride.fit.gz")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i77",))
        )
        storage = StubStorage()
        settings = make_settings()
        exit_code = main([path], client=client, storage=storage, settings=settings)
        assert exit_code == 0
        out = capsys.readouterr().out

        # The COMPRESSED bytes go to Intervals.icu, filename kept as .fit.gz
        # (the endpoint accepts a gz of fit/gpx directly; no caller-side
        # decompression for the upload).
        assert client.calls == [(compressed, "ride.fit.gz")]
        # The raw (uploaded, still-compressed) bytes are archived keyed by
        # the returned activity id.
        assert storage.saved == [("i77", compressed)]

        # The LOCAL analysis decompresses in memory (gzip magic bytes) and
        # still produces engine numbers identical to the plain-FIT run.
        method, tss = expected_load(settings, fit_bytes)
        assert method == "hr"
        assert "method=hr" in out
        assert f"tss={tss:.2f}" in out
        assert "engine_version=0.1.0" in out
        assert "modality=bike_hr" in out

    def test_fit_gz_duplicate_reports_duplicate_no_analysis(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(
            tmp_path, gz(FIT_BYTES), name="dup.fit.gz"
        )
        client = StubUploadClient(
            ActivityUploadResult(created=False, activity_ids=("i140561802",))
        )
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "duplicate" in out
        assert "load:" not in out

    def test_corrupt_gzip_reports_analysis_failure_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # gzip magic bytes followed by garbage: the upload still goes out
        # (it is the owner's file as-is), but the local analysis cannot
        # decompress it — reported with a reason, never a traceback.
        path = write_tmp_fit(tmp_path, b"\x1f\x8bnot-really-gzip", name="bad.fit.gz")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i8",))
        )
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "created activity i8" in out
        assert "analysis failed" in out


# ---------------------------------------------------------------------------
# Scope addition 2: directories, recursive discovery, non-FIT skips
# ---------------------------------------------------------------------------


class TestDirectoryDiscovery:
    def test_recursive_discovery_processes_fit_and_fit_gz_reports_skips(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        one = deep / "one.fit"
        one.write_bytes(fit_bytes)
        two = tmp_path / "x" / "two.fit.gz"
        two.parent.mkdir()
        two.write_bytes(gz(fit_bytes))
        (tmp_path / "notes.txt").write_text("not an activity")
        (tmp_path / "y").mkdir()
        (tmp_path / "y" / "other.gpx").write_bytes(b"<gpx/>")

        client = StubUploadClient(
            [
                ActivityUploadResult(created=True, activity_ids=("i1",)),
                ActivityUploadResult(created=False, activity_ids=("i100",)),
            ]
        )
        storage = StubStorage()
        exit_code = main([str(tmp_path)], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0
        out = capsys.readouterr().out

        # Discovery count is reported BEFORE processing.
        assert "discovered 2 file(s)" in out
        # Both FIT files processed (one created, one duplicate), deepest
        # nesting included; uploads carry the files' own names.
        assert (fit_bytes, "one.fit") in client.calls
        filenames = [name for _, name in client.calls]
        assert "two.fit.gz" in filenames
        assert "one.fit" in filenames
        assert "created" in out and "duplicate" in out
        assert "i100" in out  # the duplicate names the existing activity
        # Non-FIT files are skipped with a per-file reason, not failures.
        assert "notes.txt" in out
        assert "other.gpx" in out
        assert "skipped" in out

    def test_explicit_files_and_directories_can_be_mixed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        single = write_tmp_fit(tmp_path, FIT_BYTES, name="solo.fit")
        folder = tmp_path / "batch"
        folder.mkdir()
        (folder / "inner.fit").write_bytes(FIT_BYTES)
        client = StubUploadClient(
            [
                ActivityUploadResult(created=True, activity_ids=("i1",)),
                ActivityUploadResult(created=True, activity_ids=("i2",)),
            ]
        )
        storage = StubStorage()
        exit_code = main(
            [single, str(folder)],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "discovered 2 file(s)" in out
        assert {name for _, name in client.calls} == {"solo.fit", "inner.fit"}

    def test_explicit_non_fit_file_is_skipped_with_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        text = tmp_path / "readme.md"
        text.write_text("hi")
        client = StubUploadClient(ActivityUploadResult(created=True, activity_ids=("i1",)))
        storage = StubStorage()
        exit_code = main([str(text)], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0  # a skip is not a failure
        out = capsys.readouterr().out
        assert "readme.md" in out
        assert "skipped" in out
        assert client.calls == []

    def test_directory_without_fit_files_reports_zero_discovered(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.txt").write_text("x")
        client = StubUploadClient(ActivityUploadResult(created=True, activity_ids=("i1",)))
        storage = StubStorage()
        exit_code = main([str(tmp_path)], client=client, storage=storage, settings=make_settings())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "discovered 0 file(s)" in out
        assert client.calls == []


# ---------------------------------------------------------------------------
# Scope addition 3: --zip-batch N (bundle up to N files into one zip upload)
# ---------------------------------------------------------------------------


def gz(data: bytes) -> bytes:
    """Gzip fixture with a PINNED zero timestamp (never the current time).

    ``gzip.compress`` writes the wall-clock time into the gzip header, so two
    compressions of identical bytes are different artifacts one second apart:
    the only differing bytes are the MTIME at offset 4..8. This file compares
    a stored ``.gz`` member against a compressed blob, and the assertion once
    compressed a SECOND time at that moment, which made the test depend on
    whether the CLI run in between crossed a whole second. It did: CI was
    green at 11:26 and red at 13:24 on byte-identical content (same run id
    family, same tree). Pinning ``mtime=0`` makes every gzip artifact here
    reproducible byte for byte, so the comparison can never race the clock.
    """
    return gzip.compress(data, mtime=0)


def test_gz_fixture_is_time_independent() -> None:
    """Regression guard: the gzip fixtures carry a zero MTIME.

    Bytes 4..8 of a gzip stream are the MTIME field. If this ever goes back
    to the implicit ``time.time()``, a byte comparison against a re-compressed
    blob becomes clock-dependent and turns into an intermittent CI failure.
    """
    first = gz(b"payload")
    assert first[4:8] == b"\x00\x00\x00\x00"
    assert first == gz(b"payload")


def zip_members(zipped: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


class TestZipBatching:
    def test_batch_of_two_uploads_one_zip_with_both_members(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        (tmp_path / "a.fit").write_bytes(fit_bytes)
        gz_fit = gz(fit_bytes)
        (tmp_path / "b.fit.gz").write_bytes(gz_fit)
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i1", "i2"))
        )
        storage = StubStorage()
        exit_code = main(
            ["--zip-batch", "2", str(tmp_path)],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 0
        out = capsys.readouterr().out

        # ONE request for the batch, named activities-001.zip, carrying
        # exactly the two members under their original names.
        assert len(client.calls) == 1
        uploaded, filename = client.calls[0]
        assert filename == "activities-001.zip"
        members = zip_members(uploaded)
        assert set(members) == {"a.fit", "b.fit.gz"}
        assert members["a.fit"] == fit_bytes
        assert members["b.fit.gz"] == gz_fit

        # Per-batch accounting: created count and the response's ids.
        assert "activities-001.zip" in out
        assert "created 2 activity id(s)" in out
        assert "i1" in out and "i2" in out
        # Ambiguous id->file mapping: stated explicitly, archiving skipped
        # with a reason and a retry hint (never guessed).
        assert "archive: skipped" in out
        assert "unambiguously" in out
        assert "without --zip-batch" in out
        assert storage.saved == []
        # The per-file LOCAL analysis still ran from each member's own bytes.
        assert out.count("method=hr") == 2
        assert out.count("tss=") == 2
        assert "engine_version=0.1.0" in out

    def test_single_member_batch_is_unambiguous_and_archives_per_activity(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        path = write_tmp_fit(tmp_path, fit_bytes, name="solo.fit")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i5",))
        )
        storage = StubStorage()
        exit_code = main(
            ["--zip-batch", "5", path],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert len(client.calls) == 1
        assert client.calls[0][1] == "activities-001.zip"
        # One id, one member: the mapping IS unambiguous -> archive keyed
        # by the returned activity id, then analyse.
        assert storage.saved == [("i5", fit_bytes)]
        assert "archive: i5.fit" in out
        assert "archive: skipped" not in out
        assert "method=hr" in out

    def test_batch_remainder_uploads_a_second_zip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        for name in ("one.fit", "two.fit.gz", "three.fit"):
            (tmp_path / name).write_bytes(
                gz(fit_bytes) if name.endswith(".gz") else fit_bytes
            )
        client = StubUploadClient(
            [
                ActivityUploadResult(created=True, activity_ids=("i1", "i2")),
                ActivityUploadResult(created=True, activity_ids=("i3",)),
            ]
        )
        storage = StubStorage()
        exit_code = main(
            ["--zip-batch", "2", str(tmp_path)],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 0
        assert [name for _, name in client.calls] == [
            "activities-001.zip",
            "activities-002.zip",
        ]
        first_members = set(zip_members(client.calls[0][0]))
        second_members = set(zip_members(client.calls[1][0]))
        assert len(first_members) == 2 and len(second_members) == 1
        assert first_members | second_members == {
            "one.fit",
            "two.fit.gz",
            "three.fit",
        }
        # The 1:1 remainder batch archives unambiguously under the returned id.
        remainder_name = next(iter(second_members))
        assert storage.saved == [("i3", (tmp_path / remainder_name).read_bytes())]

    def test_failing_batch_reports_its_member_list_and_retry_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.fit").write_bytes(FIT_BYTES)
        (tmp_path / "b.fit").write_bytes(FIT_BYTES)
        client = StubUploadClient(IntervalsClientError(500, "boom"))
        storage = StubStorage()
        exit_code = main(
            ["--zip-batch", "2", str(tmp_path)],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "error" in out
        assert "a.fit" in out and "b.fit" in out
        assert "without --zip-batch" in out
        assert storage.saved == []

    def test_batch_duplicate_reports_members_and_no_analysis(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.fit").write_bytes(FIT_BYTES)
        (tmp_path / "b.fit").write_bytes(FIT_BYTES)
        client = StubUploadClient(
            ActivityUploadResult(created=False, activity_ids=("i101",))
        )
        storage = StubStorage()
        exit_code = main(
            ["--zip-batch", "2", str(tmp_path)],
            client=client,
            storage=storage,
            settings=make_settings(),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "duplicate" in out
        assert "i101" in out  # ids reported when the response exposes them
        assert "2" in out  # member count accounted
        assert "a.fit" in out and "b.fit" in out
        assert "load:" not in out
        assert storage.saved == []

    @pytest.mark.parametrize("bad", ["0", "-3", "2001"])
    def test_batch_size_bounds_are_validated(self, bad: str) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--zip-batch", bad, "x.fit"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# FI-4: immediate analysis from the local bytes (created files only)
# ---------------------------------------------------------------------------


class StubUploadClientAlwaysCreated(StubUploadClient):
    pass


def expected_load(settings: Settings, fit_bytes: bytes) -> tuple[str, float]:
    """Engine-produced expectation: rebuild the CLI's selection from the
    same pure primitives the tool must call (nothing invented here)."""
    from app.ingest.fit_parser import parse_fit_session_info

    info = parse_fit_session_info(fit_bytes)
    streams = parse_fit_streams(fit_bytes)
    assert info.total_timer_time_s is not None
    hr_valid = [h for h in streams["hr"] if h is not None]
    hr_avg = sum(hr_valid) / len(hr_valid)
    activity = ActivityLoadInput(
        sport="ride",
        duration_s=info.total_timer_time_s,
        power_samples=streams["power"],
        speed_samples=streams["speed"],
        distance_samples=streams["distance"],
        altitude_samples=streams["altitude"],
        hr_avg_bpm=hr_avg,
        rpe=None,
    )
    selection = select_load_method(
        activity,
        thresholds_from_settings(settings),
        coefficients=trimp_coefficients_from_settings(settings),
        np_window_samples=settings.engine_np_window_samples,
        np_min_valid_fraction=settings.engine_min_valid_fraction,
        trimp_reference_minutes=settings.engine_trimp_reference_minutes,
    )
    return selection.method, selection.tss


def expected_zones(settings: Settings, fit_bytes: bytes) -> tuple[Any, Any]:
    """Engine-produced 3-zone expectation via the shared classification rule."""
    streams = parse_fit_streams(fit_bytes)
    intensity = intensity_constants_from_settings(settings)
    modality: IntensityModalityKey = "bike_hr"  # bike sport, no usable power, usable HR
    classified = _classify_samples(
        modality,
        streams["hr"],
        ftp_watts=settings.athlete_ftp_w,
        lthr_bpm=settings.athlete_lthr_bpm,
        css_mps=settings.athlete_css_speed_mps,
    )
    zone_seconds = _weighted_zone_seconds(
        classified,
        streams["time"],
        streams["speed"],
        speed_tolerance_mps=intensity["speed_tolerance_mps"],
        gap_cap_median_multiple=intensity["gap_cap_median_multiple"],
    )
    model = three_zone_model(
        modality,
        first_threshold_pct=intensity["first_threshold_pcts"][modality],
        second_threshold_pct=intensity["second_threshold_pcts"][modality],
    )
    mapped = map_to_three_zones(model, zone_seconds)
    total = mapped.total_seconds
    percentages = (
        mapped.z1_seconds / total * 100.0,
        mapped.z2_seconds / total * 100.0,
        mapped.z3_seconds / total * 100.0,
    )
    return mapped, percentages


class TestImmediateAnalysis:
    def test_created_fixture_reports_engine_load_and_zones(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fit_bytes = FIXTURE_PATH.read_bytes()
        path = write_tmp_fit(tmp_path, fit_bytes, name="fenix.fit")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i7",))
        )
        storage = StubStorage()
        settings = make_settings()
        exit_code = main([path], client=client, storage=storage, settings=settings)
        assert exit_code == 0
        out = capsys.readouterr().out

        method, tss = expected_load(settings, fit_bytes)
        assert method == "hr"  # fixture: no power, bike sport -> HR method
        mapped, percentages = expected_zones(settings, fit_bytes)
        assert percentages[0] + percentages[1] + percentages[2] == pytest.approx(100.0)

        # Hand-derived fixture facts (documented FIT values):
        # duration = session total_timer_time = 60.363 s; the interval rule
        # (sum of consecutive-sample intervals + last sample inheriting the
        # previous interval) gives 62.0 s of classified time.
        assert "method=hr" in out
        assert f"tss={tss:.2f}" in out
        assert "engine_version=0.1.0" in out
        assert "modality=bike_hr" in out
        assert f"Z1={mapped.z1_seconds:.2f}s" in out
        assert f"Z2={mapped.z2_seconds:.2f}s" in out
        assert f"Z3={mapped.z3_seconds:.2f}s" in out
        assert (
            mapped.z1_seconds + mapped.z2_seconds + mapped.z3_seconds
            == pytest.approx(62.0)
        )
        assert f"({percentages[0]:.2f}%)" in out

    def test_unparsable_file_is_created_and_archived_but_reports_analysis_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_tmp_fit(tmp_path, b"definitely not a FIT file", name="junk.fit")
        client = StubUploadClient(
            ActivityUploadResult(created=True, activity_ids=("i3",))
        )
        storage = StubStorage()
        exit_code = main([path], client=client, storage=storage, settings=make_settings())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "created activity i3" in out
        assert "i3.fit" in out
        assert "analysis failed" in out
        assert "not a valid FIT file" in out

    def test_no_usable_load_modality_raises_with_all_skip_reasons(self) -> None:
        """A bike session with no power, no HR and no RPE: every method is
        skipped with a named reason — never a silent zero load."""
        from app.ingest.fit_parser import FitSessionInfo

        info = FitSessionInfo(
            sport="cycling",
            sub_sport=None,
            total_timer_time_s=60.0,
            total_elapsed_time_s=60.0,
        )
        empty_streams: dict[str, list[float | None]] = {
            key: [None, None, None] for key in
            ("time", "power", "hr", "speed", "cadence", "altitude", "distance")
        }
        settings = make_settings()
        with pytest.raises(ValueError, match="no applicable load method") as exc:
            analyse_fit_streams(
                info,
                empty_streams,
                engine_version=settings.engine_version,
                thresholds=thresholds_from_settings(settings),
                coefficients=trimp_coefficients_from_settings(settings),
                np_window_samples=settings.engine_np_window_samples,
                np_min_valid_fraction=settings.engine_min_valid_fraction,
                trimp_reference_minutes=settings.engine_trimp_reference_minutes,
                first_threshold_pcts=intensity_constants_from_settings(settings)[
                    "first_threshold_pcts"
                ],
                second_threshold_pcts=intensity_constants_from_settings(settings)[
                    "second_threshold_pcts"
                ],
                speed_tolerance_mps=intensity_constants_from_settings(settings)[
                    "speed_tolerance_mps"
                ],
                gap_cap_median_multiple=intensity_constants_from_settings(settings)[
                    "gap_cap_median_multiple"
                ],
            )
        assert "power" in str(exc.value)
        assert "hr" in str(exc.value)
        assert "srpe" in str(exc.value)

    def test_intensity_not_applicable_reports_reason_not_zero(self) -> None:
        """A walk session: HR load is fine, but there is no intensity
        modality for walking — an explicit reason, never a 0/0/0 split."""
        from app.ingest.fit_parser import FitSessionInfo

        info = FitSessionInfo(
            sport="walking",
            sub_sport=None,
            total_timer_time_s=60.0,
            total_elapsed_time_s=60.0,
        )
        streams: dict[str, list[float | None]] = {
            "time": [float(i) for i in range(10)],
            "power": [None] * 10,
            "hr": [120.0] * 10,
            "speed": [1.5] * 10,
            "cadence": [None] * 10,
            "altitude": [None] * 10,
            "distance": [None] * 10,
        }
        settings = make_settings()
        report = analyse_fit_streams(
            info,
            streams,
            engine_version=settings.engine_version,
            thresholds=thresholds_from_settings(settings),
            coefficients=trimp_coefficients_from_settings(settings),
            np_window_samples=settings.engine_np_window_samples,
            np_min_valid_fraction=settings.engine_min_valid_fraction,
            trimp_reference_minutes=settings.engine_trimp_reference_minutes,
            first_threshold_pcts=intensity_constants_from_settings(settings)[
                "first_threshold_pcts"
            ],
            second_threshold_pcts=intensity_constants_from_settings(settings)[
                "second_threshold_pcts"
            ],
            speed_tolerance_mps=intensity_constants_from_settings(settings)[
                "speed_tolerance_mps"
            ],
            gap_cap_median_multiple=intensity_constants_from_settings(settings)[
                "gap_cap_median_multiple"
            ],
        )
        assert report.method == "hr"
        assert report.modality is None
        assert report.z1_seconds is None and report.z2_seconds is None
        assert report.z3_seconds is None
        assert report.zone_reason is not None
        assert "walk" in report.zone_reason

    def test_unmapped_fit_sport_is_never_guessed(self) -> None:
        """An unknown/missing FIT sport cannot become an engine input."""
        from app.ingest.fit_parser import FitSessionInfo

        info = FitSessionInfo(
            sport=None, sub_sport=None, total_timer_time_s=60.0,
            total_elapsed_time_s=60.0,
        )
        streams: dict[str, list[float | None]] = {
            "time": [float(i) for i in range(10)],
            "power": [None] * 10,
            "hr": [120.0] * 10,
            "speed": [1.5] * 10,
            "cadence": [None] * 10,
            "altitude": [None] * 10,
            "distance": [None] * 10,
        }
        settings = make_settings()
        with pytest.raises(ValueError, match="sport"):
            analyse_fit_streams(
                info,
                streams,
                engine_version=settings.engine_version,
                thresholds=thresholds_from_settings(settings),
                coefficients=trimp_coefficients_from_settings(settings),
                np_window_samples=settings.engine_np_window_samples,
                np_min_valid_fraction=settings.engine_min_valid_fraction,
                trimp_reference_minutes=settings.engine_trimp_reference_minutes,
                first_threshold_pcts=intensity_constants_from_settings(settings)[
                    "first_threshold_pcts"
                ],
                second_threshold_pcts=intensity_constants_from_settings(settings)[
                    "second_threshold_pcts"
                ],
                speed_tolerance_mps=intensity_constants_from_settings(settings)[
                    "speed_tolerance_mps"
                ],
                gap_cap_median_multiple=intensity_constants_from_settings(settings)[
                    "gap_cap_median_multiple"
                ],
            )
