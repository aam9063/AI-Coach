"""Unit tests for the snapshot comparison helper itself.

The snapshot contract is the rendered IMAGE, not the PNG container's
compression choices: raw PNG bytes differ across platforms (the same
pixels compress differently with the zlib builds shipped in the Windows
and manylinux matplotlib wheels), so ``assert_png_snapshot`` must
compare decoded pixels exactly.
"""

from __future__ import annotations

import pytest

from tests.charts.snapshot import pixels_equal


def test_identical_bytes_have_identical_pixels() -> None:
    from app.charts import render_performance_manager
    from tests.charts.fixtures import PERFORMANCE_MANAGER_SAMPLES

    png = render_performance_manager(PERFORMANCE_MANAGER_SAMPLES)
    assert pixels_equal(png, png)


def test_a_different_image_is_not_equal_pixel_wise() -> None:
    from app.charts import render_performance_manager
    from tests.charts.fixtures import PERFORMANCE_MANAGER_SAMPLES

    full = render_performance_manager(PERFORMANCE_MANAGER_SAMPLES)
    # One sample fewer: a genuinely different image (fewer days drawn),
    # not a recompression of the same pixels.
    shorter = render_performance_manager(PERFORMANCE_MANAGER_SAMPLES[:-10])
    assert not pixels_equal(full, shorter)


def test_non_png_input_is_reported_not_crashed() -> None:
    with pytest.raises(ValueError, match="not a PNG"):
        pixels_equal(b"definitely not a png", b"also not a png")
