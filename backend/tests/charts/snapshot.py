"""PNG snapshot storage and comparison for chart renderers (§10, §12.7).

Chart renderers are pure functions: data in, PNG bytes out, with no DB,
no network, and no wall-clock time inside (the title carries the date
range of the data, not the render time). That purity makes snapshot
testing meaningful: same canonical input, same rendered image.

The comparison contract is PIXELS, not raw bytes: the same pixels
compress differently across platforms (the zlib builds shipped in the
Windows and manylinux matplotlib wheels produce different PNG
containers), so byte equality fails on CI while every pixel matches.
Baselines are committed PNGs; a mismatch means a genuinely different
image.

Baselines live in ``tests/charts/snapshots/<name>.png`` and are committed
so a change that alters a chart's appearance must update a file in the
same diff — visual changes can never slip through silently.

Update procedure (when a chart change is intentional):

    CHART_SNAPSHOT_UPDATE=1 uv run pytest tests/charts

    # then review the baselines (open the PNGs) and commit them together
    # with the renderer change that produced them

Without the variable a differing PNG fails with the same instructions,
and a missing baseline fails too, so a forgotten file is loud, not a
silent first-run pass.
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path

import numpy as np
import pytest
from matplotlib.image import imread

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE_ENV = "CHART_SNAPSHOT_UPDATE"

MISSING_BASELINE_MESSAGE = (
    "no PNG snapshot baseline for {name}: review the rendered chart at "
    "{path}, then commit it. Regenerate baselines on purpose with "
    "CHART_SNAPSHOT_UPDATE=1 uv run pytest tests/charts"
)

MISMATCH_MESSAGE = (
    "rendered PNG differs from the snapshot baseline for {name}. If the "
    "visual change is intentional, regenerate with CHART_SNAPSHOT_UPDATE=1 "
    "uv run pytest tests/charts and commit the new baseline together with "
    "the renderer change; otherwise fix the renderer."
)


def snapshot_path(name: str) -> Path:
    """Where the baseline for ``name`` lives."""
    return SNAPSHOT_DIR / f"{name}.png"


def pixels_equal(baseline_png: bytes, rendered_png: bytes) -> bool:
    """Decode two PNGs and compare their pixels EXACTLY.

    Raw bytes are not the contract: identical pixels recompress into
    different PNG containers across platforms (observed live: CI on
    ubuntu-latest failed byte-for-byte while every pixel matched). Decoding
    is deterministic, so exact array equality is the honest check.
    """
    for blob in (baseline_png, rendered_png):
        if blob[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not a PNG file")
    baseline = imread(io.BytesIO(baseline_png))
    rendered = imread(io.BytesIO(rendered_png))
    if baseline.shape != rendered.shape:
        return False
    return bool(np.array_equal(baseline, rendered))


def assert_png_snapshot(png: bytes, name: str) -> None:
    """Compare a rendered PNG against the committed baseline by pixels.

    ``CHART_SNAPSHOT_UPDATE=1`` rewrites the baseline and passes; the
    rewritten file still has to be reviewed and committed by a human.
    """
    path = snapshot_path(name)
    if os.environ.get(UPDATE_ENV) == "1":
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return
    if not path.exists():
        pytest.fail(MISSING_BASELINE_MESSAGE.format(name=name, path=path))
    if not pixels_equal(path.read_bytes(), png):
        pytest.fail(MISMATCH_MESSAGE.format(name=name))


def png_dimensions(png: bytes) -> tuple[int, int]:
    """Return (width, height) parsed from the PNG IHDR chunk.

    Renderers must satisfy the §10 conventions (1080 px wide), and the
    snapshot bytes alone do not say which convention a baseline encodes;
    this keeps the rule asserted explicitly.
    """
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    width, height = struct.unpack(">II", png[16:24])
    return width, height
