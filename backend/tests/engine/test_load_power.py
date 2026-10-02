"""Reference-value tests for bike power-based load (Coggan NP / IF / TSS).

Every expected value below is hand-derived from the formulas documented in
``app/engine/load.py`` (Allen & Coggan, *Training and Racing with a Power
Meter*):

    NP  = (mean over windows of (rolling 30 s mean of power)^4)^(1/4)
    IF  = NP / FTP
    TSS = (duration_s * NP * IF) / (FTP * 3600) * 100

Hand arithmetic for the varying reference series (used in three tests):

    Series: 60 per-second samples, the first 30 at 0 W, the last 30 at 300 W.
    The sliding 30 s window starts at offsets k = 0..30 (31 windows). The
    window starting at k contains (30 - k) samples of 0 W and k samples of
    300 W, so its mean is 300*k/30 = 10k: means are 0, 10, 20, ..., 300.

    mean of fourth powers = (1/31) * sum_{k=0}^{30} (10k)^4
                          = (10^4 / 31) * sum_{k=0}^{30} k^4
    sum_{k=0}^{n} k^4     = n(n+1)(2n+1)(3n^2 + 3n - 1) / 30
                          = 30*31*61*2789 / 30 = 31*61*2789 = 5,273,999
    mean of fourth powers = 10,000 * 5,273,999 / 31
                          = 52,739,990,000 / 31 = 1,701,290,322.580...
    NP                    = 1,701,290,322.580^(1/4) = 203.0928... W

    (203^4 = 1,698,181,681 and 204^4 = 1,731,891,456 bracket the value.)
    Average power over the same series = (30*0 + 30*300) / 60 = 150 W, so
    NP (203.09) > AP (150): the variability bonus required by Coggan.

    Same series with the first sample missing (None): with the strict
    default (only fully complete windows qualify), window k = 0 is skipped
    and 30 windows remain (means 10k, k = 1..30):

    mean of fourth powers = 10,000 * 5,273,999 / 30
                          = 52,739,990,000 / 30 = 1,757,999,666.666...
    NP                    = 1,757,999,666.666^(1/4) = 204.7645... W

    TSS reference (1 h at 200 W with FTP 180 W):
    IF  = 200 / 180 = 1.111111... = 10/9
    TSS = (3600 * 200 * (10/9)) / (180 * 3600) * 100
        = (200/180)^2 * 100 = (10/9)^2 * 100 = 10000/81 = 123.456790123...
    (equals 100 * IF^2 * hours, the standard Coggan identity for 1 h.)
"""

from collections.abc import Sequence
from typing import Final

import pytest

from app.engine.load import (
    BikePowerLoad,
    bike_power_load,
    intensity_factor,
    normalized_power,
    power_tss,
)

FTP: Final = 180.0


class TestNormalizedPower:
    def test_constant_200w_one_hour_is_exactly_200(self) -> None:
        # Every 30 s rolling window has mean 200; mean of 200^4 over all
        # windows is 200^4; fourth root returns 200. Sanity anchor case.
        samples: list[float | None] = [200.0] * 3600
        assert normalized_power(samples) == pytest.approx(200.0, rel=1e-9)

    def test_step_series_hand_derived(self) -> None:
        # 30 s at 0 W then 30 s at 300 W: window means 10k, k = 0..30 (see
        # module docstring). Mean of fourth powers = 52,736,990,000 / 31.
        samples: Sequence[float | None] = [0.0] * 30 + [300.0] * 30
        expected = (52_739_990_000 / 31) ** 0.25
        assert expected == pytest.approx(203.0928, rel=1e-4)  # docstring sanity
        assert normalized_power(samples) == pytest.approx(expected, rel=1e-12)

    def test_np_exceeds_average_power_for_variable_series(self) -> None:
        # Coggan's variability bonus: same series, AP = 150 W < NP = 203.09 W.
        samples: Sequence[float | None] = [0.0] * 30 + [300.0] * 30
        np_value = normalized_power(samples)
        ap_value = sum(s for s in samples if s is not None) / len(samples)
        assert ap_value == pytest.approx(150.0)
        assert np_value > ap_value

    def test_np_equals_average_power_for_constant_series(self) -> None:
        samples: list[float | None] = [200.0] * 120
        assert normalized_power(samples) == pytest.approx(
            sum(s for s in samples if s is not None) / len(samples)
        )

    def test_missing_sample_skips_incomplete_windows_by_default(self) -> None:
        # Same step series as above with sample 0 missing (None). Strict
        # default min_valid_fraction=1.0 skips window k = 0 only, leaving
        # means 10k, k = 1..30: NP = (52,736,990,000 / 30)^(1/4) = 204.766 W.
        samples: list[float | None] = [None] + [0.0] * 29 + [300.0] * 30
        expected = (52_739_990_000 / 30) ** 0.25
        assert expected == pytest.approx(204.7645, rel=1e-4)  # docstring sanity
        assert normalized_power(samples) == pytest.approx(expected, rel=1e-12)

    def test_min_valid_fraction_keeps_partially_gapped_window(self) -> None:
        # 30 s: first 15 samples missing, last 15 at 300 W. With
        # min_valid_fraction=0.5 the single window qualifies (15/30 = 0.5)
        # and its mean is computed over the valid samples only: 300 W.
        samples: list[float | None] = [None] * 15 + [300.0] * 15
        assert normalized_power(samples, min_valid_fraction=0.5) == pytest.approx(
            300.0, rel=1e-12
        )

    def test_strict_default_rejects_partially_gapped_window(self) -> None:
        # Same 30-sample series at the strict default (1.0): the only window
        # is incomplete, so there is no qualifying window -> clear ValueError.
        samples: list[float | None] = [None] * 15 + [300.0] * 15
        with pytest.raises(ValueError, match="no qualifying"):
            normalized_power(samples)

    def test_min_valid_fraction_bounds_are_validated(self) -> None:
        samples: list[float | None] = [200.0] * 60
        with pytest.raises(ValueError, match="min_valid_fraction"):
            normalized_power(samples, min_valid_fraction=0.0)
        with pytest.raises(ValueError, match="min_valid_fraction"):
            normalized_power(samples, min_valid_fraction=1.5)

    def test_short_file_falls_back_to_average_power(self) -> None:
        # Fewer samples than the 30 s window: NP cannot be smoothed, so the
        # documented fallback is the average of the valid samples.
        # (100+200+100+200+300+100+200+100+200+300) / 10 = 1800 / 10 = 180.
        samples: list[float | None] = [
            100.0, 200.0, 100.0, 200.0, 300.0,
            100.0, 200.0, 100.0, 200.0, 300.0,
        ]
        assert normalized_power(samples) == pytest.approx(180.0)

    def test_short_file_with_gaps_falls_back_to_valid_average(self) -> None:
        # Same short file with two missing samples: fallback averages the
        # eight valid samples: (200+100+200+300+100+200+100+200) / 8
        # = 1400 / 8 = 175.
        samples: list[float | None] = [
            None, 200.0, 100.0, 200.0, 300.0,
            100.0, 200.0, 100.0, 200.0, None,
        ]
        assert normalized_power(samples) == pytest.approx(1400.0 / 8.0)

    def test_no_usable_data_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable power data"):
            normalized_power([])
        with pytest.raises(ValueError, match="no usable power data"):
            normalized_power([None, None, None])


class TestIntensityFactor:
    def test_reference_200w_over_ftp_180(self) -> None:
        # IF = NP / FTP = 200 / 180 = 10/9 = 1.111111...
        assert intensity_factor(200.0, FTP) == pytest.approx(10.0 / 9.0, rel=1e-12)

    def test_rejects_non_positive_ftp(self) -> None:
        with pytest.raises(ValueError, match="FTP"):
            intensity_factor(200.0, 0.0)
        with pytest.raises(ValueError, match="FTP"):
            intensity_factor(200.0, -180.0)

    def test_rejects_negative_np(self) -> None:
        with pytest.raises(ValueError, match="NP"):
            intensity_factor(-1.0, FTP)


class TestPowerTss:
    def test_reference_1h_at_200w_ftp_180(self) -> None:
        # TSS = (3600 * 200 * (200/180)) / (180 * 3600) * 100
        #     = (200/180)^2 * 100 = 10000/81 = 123.456790123...
        assert power_tss(3600.0, 200.0, FTP) == pytest.approx(
            10_000.0 / 81.0, rel=1e-12
        )

    def test_one_hour_at_ftp_scores_exactly_100(self) -> None:
        # 1 h at NP = FTP: IF = 1, TSS = 100 (Coggan normalisation).
        assert power_tss(3600.0, FTP, FTP) == pytest.approx(100.0)

    def test_tss_scales_linearly_with_duration(self) -> None:
        # 30 min at NP = FTP is half of the 1 h reference: TSS = 50.
        assert power_tss(1800.0, FTP, FTP) == pytest.approx(50.0)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            power_tss(0.0, 200.0, FTP)
        with pytest.raises(ValueError, match="duration"):
            power_tss(-60.0, 200.0, FTP)

    def test_rejects_non_positive_ftp(self) -> None:
        with pytest.raises(ValueError, match="FTP"):
            power_tss(3600.0, 200.0, 0.0)

    def test_rejects_negative_np(self) -> None:
        with pytest.raises(ValueError, match="NP"):
            power_tss(3600.0, -200.0, FTP)


class TestBikePowerLoad:
    def test_reference_1h_at_200w_ftp_180(self) -> None:
        # Constant 200 W for 1 h, FTP 180:
        #   NP = 200 (constant series), IF = 10/9, TSS = 10000/81 = 123.45679.
        result = bike_power_load([200.0] * 3600, duration_s=3600.0, ftp=FTP)
        assert isinstance(result, BikePowerLoad)
        assert result.normalized_power == pytest.approx(200.0, rel=1e-9)
        assert result.intensity_factor == pytest.approx(10.0 / 9.0, rel=1e-9)
        assert result.tss == pytest.approx(10_000.0 / 81.0, rel=1e-9)

    def test_short_file_uses_average_power_fallback(self) -> None:
        # 10 valid samples at 180 W average (see TestNormalizedPower), 10 s
        # duration: IF = 180/180 = 1, TSS = 10 * 180 * 1 / (180*3600) * 100
        # = 10/3600 * 100 = 0.27777...
        samples: list[float | None] = [
            100.0, 200.0, 100.0, 200.0, 300.0,
            100.0, 200.0, 100.0, 200.0, 300.0,
        ]
        result = bike_power_load(samples, duration_s=10.0, ftp=FTP)
        assert result.normalized_power == pytest.approx(180.0)
        assert result.intensity_factor == pytest.approx(1.0)
        assert result.tss == pytest.approx(10.0 / 36.0, rel=1e-12)

    def test_propagates_no_usable_data_error(self) -> None:
        with pytest.raises(ValueError, match="no usable power data"):
            bike_power_load([None, None], duration_s=2.0, ftp=FTP)

    def test_rejects_non_positive_duration_and_ftp(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            bike_power_load([200.0] * 60, duration_s=0.0, ftp=FTP)
        with pytest.raises(ValueError, match="FTP"):
            bike_power_load([200.0] * 60, duration_s=60.0, ftp=0.0)
