"""The unknown-``.env``-key warning: tolerant of extras, loud about typos.

Context (2026-10-14): ``Settings`` tolerates extra ``.env`` entries because
``LANGFUSE_BASE_URL`` -- a legitimate variable of the Langfuse SDK -- bricked
every process under ``extra="forbid"``. These tests pin the other half of that
trade-off: an unknown key is named out loud, and nothing about it can fail a
process or a gate.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.env_audit import DEFAULT_ENV_PATHS, unknown_env_keys, warn_unknown_env_keys


def test_unknown_key_is_named_with_its_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "OPENAI_API_KEY=x\nINTERVALS_API_KEYY=oops\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        found = warn_unknown_env_keys([env])
    assert found == {str(env): ["INTERVALS_API_KEYY"]}
    assert "INTERVALS_API_KEYY" in caplog.text
    assert str(env) in caplog.text


def test_known_keys_and_comments_produce_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nOPENAI_API_KEY=x\nDATABASE_URL=y\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        found = warn_unknown_env_keys([env])
    assert found == {}
    # Targeted, not "caplog is empty": another library's warning must not fail
    # this test, and the absence of a mention is the claim being made.
    assert str(env) not in caplog.text


def test_missing_file_is_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        found = warn_unknown_env_keys([tmp_path / "missing.env"])
    assert found == {}
    assert "unknown key" not in caplog.text


def test_each_file_is_reported_separately(tmp_path: Path) -> None:
    first = tmp_path / "first.env"
    first.write_text("NOPE_FIRST=1\n", encoding="utf-8")
    second = tmp_path / "second.env"
    second.write_text("KEY_WITHOUT_EQUALS\nNOPE_SECOND=2\n", encoding="utf-8")
    assert unknown_env_keys([first, second]) == {
        str(first): ["NOPE_FIRST"],
        str(second): ["NOPE_SECOND"],
    }


def test_default_paths_are_the_two_developer_files() -> None:
    """The audit looks where a developer's process actually reads."""
    assert (Path(".env"), Path("backend/.env")) == DEFAULT_ENV_PATHS
