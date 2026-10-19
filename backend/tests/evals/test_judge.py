"""Tests for the tone/clarity LLM-as-judge path (EVAL-9, §13).

The judge is REAL-model-only (§13: qualitative dimensions only), so the
deterministic pins here are its CONTRACT: the judge system prompt's hard
limits, the verdict parsing (malformed output never passes silently),
the agreement-rate math, and the calibration runner's skip behavior
when no provider is configured (CI has no LLM key; the deterministic
§13 gates do not need one). The judge's actual quality is calibrated by
the owner running ``EVAL_RUN_JUDGE=1`` with a real provider and
reviewing the reported agreement rate.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.evals.judge import JUDGE_SYSTEM_PROMPT, agreement_rate, judge_reply


class TestJudgeSystemPromptLimits:
    def test_the_judge_is_restricted_to_tone_and_clarity(self) -> None:
        lowered = JUDGE_SYSTEM_PROMPT.lower()
        for dimension in ("tono", "claridad"):
            assert dimension in lowered
        # The hard prohibition: factual/numeric evaluation is OUT of scope.
        assert "números" in lowered or "números" in lowered
        assert "no evalúes nunca" in lowered

    def test_the_judge_knows_citations_are_never_displayed(self) -> None:
        # The WA-9/Log-L2 rule the judge must not fight against.
        assert "citas" in JUDGE_SYSTEM_PROMPT.lower()
        assert "nunca muestran" in JUDGE_SYSTEM_PROMPT.lower()


class TestJudgeReplyParsing:
    def test_a_clean_json_verdict_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeJudgeAgent:
            def __call__(self, _message: str) -> str:
                return '{"verdict": "acceptable", "reason": "claro y cálido"}'

        monkeypatch.setattr(
            "tests.evals.judge.Agent", lambda **_kwargs: FakeJudgeAgent()
        )
        parsed = judge_reply("Gran sesión, sigue así.", _fake_settings(monkeypatch))
        assert parsed["verdict"] == "acceptable"

    def test_json_wrapped_in_prose_or_fences_parses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeJudgeAgent:
            def __call__(self, _message: str) -> str:
                return '```json\n{"verdict": "unacceptable", "reason": "confuso"}\n```'

        monkeypatch.setattr(
            "tests.evals.judge.Agent", lambda **_kwargs: FakeJudgeAgent()
        )
        parsed = judge_reply("hacer mas cosas", _fake_settings(monkeypatch))
        assert parsed["verdict"] == "unacceptable"

    def test_a_non_json_reply_raises_never_passes_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeJudgeAgent:
            def __call__(self, _message: str) -> str:
                return "Me parece una buena respuesta en general."

        monkeypatch.setattr(
            "tests.evals.judge.Agent", lambda **_kwargs: FakeJudgeAgent()
        )
        with pytest.raises(ValueError, match="not JSON"):
            judge_reply("algo", _fake_settings(monkeypatch))

    def test_an_unknown_verdict_value_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeJudgeAgent:
            def __call__(self, _message: str) -> str:
                return '{"verdict": "excellent", "reason": "genial"}'

        monkeypatch.setattr(
            "tests.evals.judge.Agent", lambda **_kwargs: FakeJudgeAgent()
        )
        with pytest.raises(ValueError, match="malformed"):
            judge_reply("algo", _fake_settings(monkeypatch))


class TestAgreementRate:
    def test_all_matching_labels_agree_fully(self) -> None:
        verdicts = [
            {"verdict": "acceptable", "reason": ""},
            {"verdict": "unacceptable", "reason": ""},
        ]
        assert agreement_rate(verdicts, ["acceptable", "unacceptable"]) == 1.0

    def test_a_disagreement_counts_against(self) -> None:
        verdicts = [{"verdict": "acceptable", "reason": ""}]
        assert agreement_rate(verdicts, ["unacceptable"]) == 0.0

    def test_an_empty_label_set_agrees_fully(self) -> None:
        assert agreement_rate([], []) == 1.0


class TestCalibrationRunner:
    def test_the_owner_label_seed_set_exists_and_is_wellformed(self) -> None:
        # The seed set the judge is calibrated against lives in the repo;
        # it must exist and carry a label per entry.
        import yaml

        from tests.evals.judge import _JUDGE_LABELS_PATH

        data = yaml.safe_load(_JUDGE_LABELS_PATH.read_text(encoding="utf-8"))
        labeled = data["answers"]
        assert len(labeled) >= 6  # a small set, per §13
        for entry in labeled:
            assert entry["label"] in ("acceptable", "unacceptable")
            assert entry["reply"]

    def test_the_real_judge_path_skips_without_a_provider(self) -> None:
        # Without any API key the provider factory fails: the calibration
        # path must SKIP (never silently pass) when unconfigurable.
        settings = _settings_without_keys()
        with pytest.raises(Exception) as excinfo:
            judge_reply("algo", settings)
        assert excinfo.errisinstance((ValueError, Exception))


def _fake_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.core.settings import Settings

    return Settings(_env_file=None, llm_provider="fake", openai_api_key="")


def _settings_without_keys() -> Any:
    from app.core.settings import Settings

    return Settings(_env_file=None, llm_provider="openai", openai_api_key="")
