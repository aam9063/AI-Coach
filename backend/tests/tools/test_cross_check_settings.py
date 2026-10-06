"""Cross-check tolerances are Settings-sourced with CLI override (LOAD-11).

The CLI flags default to ``None`` and fall back to the owner-configured
Settings values (whose defaults are the owner-agreed §12.3 tolerances and
the documented revision threshold); an explicit flag wins.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.settings import Settings
from app.tools.cross_check_pmc import _resolve_tolerances, build_parser


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


class TestCliDefaultsComeFromSettings:
    def test_tolerance_flags_default_to_none(self) -> None:
        parser = build_parser()
        assert parser.get_default("tolerance") is None
        assert parser.get_default("absolute_tolerance") is None
        assert parser.get_default("revision_threshold") is None

    def test_resolver_falls_back_to_settings(self) -> None:
        config = _settings(
            engine_cross_check_relative_tolerance=0.2,
            engine_cross_check_absolute_tolerance=1.5,
            engine_cross_check_revision_threshold=0.4,
        )
        args = build_parser().parse_args(["--days", "200"])
        assert _resolve_tolerances(args, config) == (0.2, 1.5, 0.4)

    def test_resolver_uses_the_documented_defaults(self) -> None:
        args = build_parser().parse_args(["--days", "200"])
        assert _resolve_tolerances(args, _settings()) == (0.10, 0.5, 0.25)

    def test_explicit_cli_flags_win_over_settings(self) -> None:
        config = _settings(engine_cross_check_relative_tolerance=0.2)
        args = build_parser().parse_args(
            ["--days", "200", "--tolerance", "0.05", "--revision-threshold", "0.3"]
        )
        assert _resolve_tolerances(args, config) == (0.05, 0.5, 0.3)

    def test_resolver_rejects_non_positive_settings_values(self) -> None:
        args = build_parser().parse_args(["--days", "200"])
        with pytest.raises(ValueError, match="tolerance"):
            _resolve_tolerances(args, _settings(engine_cross_check_relative_tolerance=0.0))
