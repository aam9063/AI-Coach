"""Stream-level speed cleaning (data quality, pure engine).

Established on the owner's REAL ingested history: even after the
impossible-average activities are filtered out, the stored speed streams
carry spikes that survive any single generous cap (the parent's
measurement: a best 5-minute effort of 3:01.5/km = 5.53 m/s for an athlete
who jogs easy runs at 6-8 min/km). The cleaning rule caps implausibly FAST
samples per activity at

    cap = min(physiological ceiling, cap_percentile of the activity's own
              strictly positive samples)

so one spike cannot dominate a best-effort statistic: the percentile part
follows the activity's own distribution (sparse spikes are far rarer than
the top 1%), the ceiling part backstops activities whose whole tail is
corrupt. See ``app.engine.quality`` for the full documented rule and
provenance of every constant.
"""

from __future__ import annotations

import pytest

from app.engine.quality import (
    DEFAULT_SPEED_CAP_PERCENTILE,
    RUN_SPEED_SAMPLE_CEILING_MPS,
    clean_speed_stream,
)

# CORRECTION (2026-10, finishing the failed attempt): the mean-maximal speed
# curve lives in ``app.engine.zones`` (ZON-5), not ``app.engine.load`` — the
# original import here pointed at the wrong module and could never run GREEN.
from app.engine.zones import mean_maximal_speed_curve


def series(*chunks: tuple[int, float]) -> list[float | None]:
    """Concatenate (count, value) chunks into one sample list."""
    out: list[float | None] = []
    for count, value in chunks:
        out.extend([value] * count)
    return out


class TestSparseSpikesCannotDominate:
    """The parent's core requirement: injected spikes leave the best effort
    identical to the same series WITHOUT the spikes."""

    def make_clean(self) -> list[float | None]:
        # 3000 s steady easy run at 2.5 m/s.
        return series((3000, 2.5))

    def make_spiked(self) -> list[float | None]:
        # Same run with 5 injected GPS spikes at 9.0 m/s (0.17% of samples).
        out = self.make_clean()
        for index in (100, 700, 1400, 2100, 2800):
            out[index] = 9.0
        return out

    def best(self, samples: list[float | None], duration_s: int) -> float:
        curve = mean_maximal_speed_curve(
            samples, durations_s=[duration_s], min_valid_fraction=1.0
        )
        return curve[duration_s]

    def test_cleaned_spiked_series_matches_clean_series(self) -> None:
        clean_best = self.best(self.make_clean(), 300)
        spiked = clean_speed_stream(self.make_spiked())
        cleaned_best = self.best(list(spiked.samples), 300)

        assert cleaned_best == pytest.approx(clean_best)

    def test_uncleaned_spiked_series_is_inflated(self) -> None:
        """Sanity: the RAW spiked series really is corrupted.

        CORRECTION (2026-10, finishing the failed attempt): the original
        assertion expected a 9.0 m/s best 300-s MEAN from five 1-sample
        spikes — arithmetically impossible (a single spike lifts a 300-s
        window mean to at most (299 x 2.5 + 9.0) / 300 ~= 2.52). The spike
        is fully visible in the 1-SECOND best effort, so that is what the
        sanity check asserts; the intent (raw spiked series is corrupted,
        clean series is not) is unchanged.
        """
        assert self.best(self.make_spiked(), 1) == pytest.approx(9.0)
        assert self.best(self.make_clean(), 1) == pytest.approx(2.5)

    def test_reported_counts_and_cap(self) -> None:
        spiked = clean_speed_stream(self.make_spiked())

        assert spiked.n_samples_capped == 5
        assert spiked.n_positive_samples == 3000
        assert spiked.cap_mps == pytest.approx(2.5)
        assert list(spiked.samples)[100] == pytest.approx(2.5)


class TestGenuineFastSegmentPreserved:
    """A genuinely fast sustained segment survives the cleaning."""

    def make_with_segment(self, *, spikes: bool) -> list[float | None]:
        # 2700 s easy at 2.5 m/s plus a 300 s sustained effort at 4.5 m/s
        # (10% of the samples — far above the 1% tail the cap touches).
        out = series((1500, 2.5), (300, 4.5), (1200, 2.5))
        if spikes:
            for index in (10, 800, 2500):
                out[index] = 9.0
        return out

    def test_fast_segment_survives_spike_cleaning(self) -> None:
        no_spikes = clean_speed_stream(self.make_with_segment(spikes=False))
        with_spikes = clean_speed_stream(self.make_with_segment(spikes=True))

        for cleaned in (no_spikes, with_spikes):
            curve = mean_maximal_speed_curve(
                list(cleaned.samples), durations_s=[300], min_valid_fraction=1.0
            )
            assert curve[300] == pytest.approx(4.5)


class TestCeilingBackstop:
    """The absolute ceiling caps activities whose whole tail is corrupt."""

    def test_ceiling_caps_dense_corruption(self) -> None:
        # A corrupt stream where MOST samples sit at 14.0 m/s: the percentile
        # alone would follow the corruption, the physiological ceiling stops it.
        corrupt = series((500, 14.0), (100, 2.5))
        cleaned = clean_speed_stream(corrupt)

        assert cleaned.cap_mps == RUN_SPEED_SAMPLE_CEILING_MPS
        assert all(
            s is None or s <= RUN_SPEED_SAMPLE_CEILING_MPS for s in cleaned.samples
        )
        assert max(s for s in cleaned.samples if s is not None) == (
            RUN_SPEED_SAMPLE_CEILING_MPS
        )


class TestPassthroughAndUntouched:
    """None-gaps, zeros and sub-cap samples pass through untouched."""

    def test_none_and_zero_entries_unchanged(self) -> None:
        raw: list[float | None] = [None, 0.0, 2.5, None, 2.6, 8.0, 2.4]
        cleaned = clean_speed_stream(raw)

        assert list(cleaned.samples)[:2] == [None, 0.0]
        assert list(cleaned.samples)[3] is None
        assert list(cleaned.samples)[4] == pytest.approx(2.6)
        assert list(cleaned.samples)[5] == pytest.approx(cleaned.cap_mps)
        assert list(cleaned.samples)[6] == pytest.approx(2.4)


class TestValidation:
    """Documented errors, never silent defaults (engine convention)."""

    def test_empty_sequence_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable speed data"):
            clean_speed_stream([])

    def test_all_none_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable speed data"):
            clean_speed_stream([None, None])

    def test_no_positive_sample_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable speed data"):
            clean_speed_stream([0.0, 0.0])

    def test_percentile_bounds_enforced(self) -> None:
        for bad in (0.0, 1.0, -0.5, 1.5):
            with pytest.raises(ValueError, match="cap_percentile"):
                clean_speed_stream([2.5, 2.6], cap_percentile=bad)

    def test_nonpositive_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            clean_speed_stream([2.5, 2.6], ceiling_mps=0.0)

    def test_unknown_sport_without_ceiling_raises(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            clean_speed_stream([2.5, 2.6], sport="elliptical")


class TestConfigurable:
    """The percentile is an explicit parameter (documented trade-off)."""

    def test_percentile_follows_own_distribution(self) -> None:
        raw = series((90, 2.5), (10, 5.0))
        cleaned = clean_speed_stream(raw, cap_percentile=0.5)

        assert cleaned.cap_mps == pytest.approx(2.5)
        assert cleaned.n_samples_capped == 10

    def test_default_percentile_constant(self) -> None:
        assert DEFAULT_SPEED_CAP_PERCENTILE == 0.99
