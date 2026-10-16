"""PNG snapshot storage and comparison for chart renderers (§10, §12.7).

Chart renderers are pure functions: data in, PNG bytes out, with no DB,
no network, and no wall-clock time inside (the title carries the date
range of the data, not the render time). That purity makes byte-for-byte
snapshot testing meaningful: same canonical input, same bytes.

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

import os
import struct
from pathlib import Path

import pytest

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


def assert_png_snapshot(png: bytes, name: str) -> None:
    """Compare rendered PNG bytes against the committed baseline.

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
    if path.read_bytes() != png:
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
