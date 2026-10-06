"""Threshold change detection proposes, never applies (ZON-9, PROJECT_BRIEF
section 7.3).

Contract under test (``app.engine.zones.propose_threshold_change``):

- A new effort exceeding the current model value by MORE than a
  configurable margin returns a typed :class:`ThresholdChangeProposal`
  carrying the proposed value, the prior (current) value, the delta, the
  margin used and the evidence needed to justify it.
- A non-exceeding effort returns an explicit :class:`NoThresholdChange`
  outcome with a machine-readable reason — never ``None``, never a raise.
- The function is INCAPABLE of applying anything: the result types are
  frozen, carry no mutating capability, and the engine module defines no
  apply/commit/persist entry point at all (purity, section 6; the
  athlete-confirmed application lands with ZON-10/Feature 6 OUTSIDE the
  engine).
- Inputs are validated with clear ``ValueError`` s: positive values,
  margin >= 0; the exact-margin boundary is tested on both sides
  (strictly more than the margin proposes; exactly at or below does not).
- Every metric the model actually produces is covered with its stable
  machine-readable key: ``ftp_watts``, ``cp_watts``, ``cs_mps``,
  ``css_mps``.
"""

import dataclasses

import pytest

from app.engine import zones
from app.engine.zones import (
    DEFAULT_THRESHOLD_CHANGE_MARGIN,
    THRESHOLD_METRIC_KEYS,
    THRESHOLD_NO_PROPOSAL_REASONS,
    NoThresholdChange,
    ThresholdChangeProposal,
    propose_threshold_change,
)


class TestProposalWhenMarginExceeded:
    def test_ftp_increase_beyond_margin_is_a_proposal(self) -> None:
        result = propose_threshold_change(
            metric="ftp_watts",
            current_value=180.0,
            new_value=195.0,  # +8.33% > the 5% default margin
            evidence="best 20-min effort 195 W on 2026-03-01",
        )
        assert isinstance(result, ThresholdChangeProposal)
        assert result.metric == "ftp_watts"
        assert result.prior_value == 180.0
        assert result.proposed_value == 195.0
        assert result.delta == pytest.approx(15.0)
        assert result.relative_delta == pytest.approx(15.0 / 180.0)
        assert result.margin == pytest.approx(DEFAULT_THRESHOLD_CHANGE_MARGIN)
        assert result.unit == "W"
        assert result.status == "proposal_not_applied"
        # §7.3: the proposal object carries evidence and prior value.
        assert "best 20-min effort 195 W on 2026-03-01" in result.evidence
        assert "180.0" in result.evidence and "195.0" in result.evidence

    @pytest.mark.parametrize(
        ("metric", "current", "new"),
        [
            ("ftp_watts", 180.0, 195.0),  # +8.33%
            ("cp_watts", 250.0, 280.0),  # +12.0%
            ("cs_mps", 4.0, 4.3),  # +7.5%
            ("css_mps", 0.833333, 0.9),  # +8.0%
        ],
    )
    def test_every_produced_metric_can_propose(
        self, metric: str, current: float, new: float
    ) -> None:
        result = propose_threshold_change(
            metric=metric,  # type: ignore[arg-type]
            current_value=current,
            new_value=new,
        )
        assert isinstance(result, ThresholdChangeProposal)
        assert result.metric == metric
        assert result.metric in THRESHOLD_METRIC_KEYS
        assert result.prior_value == current
        assert result.proposed_value == new

    def test_default_margin_is_the_documented_owner_choice(self) -> None:
        # OWNER CHOICE (ZON-11): 5% relative, pending owner confirmation;
        # mirrored in Settings as ENGINE_THRESHOLD_CHANGE_MARGIN.
        assert DEFAULT_THRESHOLD_CHANGE_MARGIN == 0.05

    def test_custom_margin_is_used_not_the_default(self) -> None:
        # +7.5% is inside a 10% margin: no proposal.
        below = propose_threshold_change(
            metric="cs_mps", current_value=4.0, new_value=4.3, margin=0.10
        )
        assert isinstance(below, NoThresholdChange)
        assert below.margin == pytest.approx(0.10)
        # +12.5% exceeds a 10% margin: proposal, reporting the 0.10 margin.
        above = propose_threshold_change(
            metric="cs_mps", current_value=4.0, new_value=4.5, margin=0.10
        )
        assert isinstance(above, ThresholdChangeProposal)
        assert above.margin == pytest.approx(0.10)


class TestExactMarginBoundary:
    def test_exactly_at_margin_is_not_proposed(self) -> None:
        # 189.0 = 180.0 * 1.05: exactly the 5% margin. "More than the
        # margin" is strict, so the exact boundary does NOT propose.
        result = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=189.0, margin=0.05
        )
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "margin_not_exceeded"

    def test_just_above_margin_is_proposed(self) -> None:
        # 189.01 W is a hair beyond the boundary (beyond the boundary
        # comparison tolerance too, mirroring the _same_boundary rule).
        result = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=189.01, margin=0.05
        )
        assert isinstance(result, ThresholdChangeProposal)

    def test_just_below_margin_is_not_proposed(self) -> None:
        result = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=188.99, margin=0.05
        )
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "margin_not_exceeded"

    def test_zero_margin_any_increase_proposes(self) -> None:
        result = propose_threshold_change(
            metric="cs_mps", current_value=4.0, new_value=4.01, margin=0.0
        )
        assert isinstance(result, ThresholdChangeProposal)
        assert result.margin == pytest.approx(0.0)

    def test_zero_margin_no_increase_is_not_proposed(self) -> None:
        result = propose_threshold_change(
            metric="cs_mps", current_value=4.0, new_value=4.0, margin=0.0
        )
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "no_improvement"


class TestExplicitNoProposalOutcome:
    def test_decrease_is_an_explicit_no_proposal_never_none(self) -> None:
        result = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=170.0
        )
        assert result is not None
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "no_improvement"
        assert result.reason in THRESHOLD_NO_PROPOSAL_REASONS
        assert result.prior_value == 180.0
        assert result.proposed_value == 170.0

    def test_equal_value_is_no_improvement(self) -> None:
        result = propose_threshold_change(
            metric="cp_watts", current_value=250.0, new_value=250.0
        )
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "no_improvement"

    def test_within_margin_reason_is_machine_readable(self) -> None:
        result = propose_threshold_change(
            metric="css_mps", current_value=0.833333, new_value=0.85
        )
        assert isinstance(result, NoThresholdChange)
        assert result.reason == "margin_not_exceeded"
        assert isinstance(result.detail, str) and result.detail


class TestProposalIsIncapableOfApplying:
    """§7.3 hard requirement: proposals never apply; the engine cannot."""

    def test_proposal_is_frozen(self) -> None:
        proposal = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=195.0
        )
        assert isinstance(proposal, ThresholdChangeProposal)
        with pytest.raises(dataclasses.FrozenInstanceError):
            proposal.proposed_value = 999.0  # type: ignore[misc]

    def test_no_proposal_is_frozen(self) -> None:
        no = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=170.0
        )
        assert isinstance(no, NoThresholdChange)
        with pytest.raises(dataclasses.FrozenInstanceError):
            no.reason = "margin_not_exceeded"  # type: ignore[misc]

    def test_proposal_has_no_mutating_capability(self) -> None:
        proposal = propose_threshold_change(
            metric="cp_watts", current_value=250.0, new_value=280.0
        )
        assert isinstance(proposal, ThresholdChangeProposal)
        assert not hasattr(proposal, "apply")
        callables = [
            name
            for name in dir(proposal)
            if not name.startswith("_") and callable(getattr(proposal, name))
        ]
        assert callables == [], f"proposal exposes callables: {callables}"

    def test_engine_module_defines_no_apply_persist_entry_point(self) -> None:
        # The proposal flow's application step (ZON-10/Feature 6) lives
        # OUTSIDE the pure engine; no engine symbol may even look like it.
        forbidden = ("apply", "commit", "persist", "save", "write", "set_")
        offenders = [
            name
            for name, value in vars(zones).items()
            if not name.startswith("_") and name.startswith(forbidden)
        ]
        assert offenders == []

    def test_prior_value_reported_unchanged(self) -> None:
        proposal = propose_threshold_change(
            metric="css_mps", current_value=0.833333, new_value=0.9
        )
        assert isinstance(proposal, ThresholdChangeProposal)
        assert proposal.prior_value == 0.833333
        no = propose_threshold_change(
            metric="css_mps", current_value=0.9, new_value=0.833333
        )
        assert isinstance(no, NoThresholdChange)
        assert no.prior_value == 0.9


class TestValidation:
    @pytest.mark.parametrize("metric", ["ftp", "watts", "FTP_WATTS", "pace_5k", ""])
    def test_unknown_metric_raises(self, metric: str) -> None:
        with pytest.raises(ValueError, match="metric"):
            propose_threshold_change(
                metric=metric,  # type: ignore[arg-type]
                current_value=180.0,
                new_value=195.0,
            )

    @pytest.mark.parametrize("current", [0.0, -180.0])
    def test_non_positive_current_value_raises(self, current: float) -> None:
        with pytest.raises(ValueError, match="current_value"):
            propose_threshold_change(
                metric="ftp_watts", current_value=current, new_value=195.0
            )

    @pytest.mark.parametrize("new", [0.0, -5.0])
    def test_non_positive_new_value_raises(self, new: float) -> None:
        with pytest.raises(ValueError, match="new_value"):
            propose_threshold_change(
                metric="ftp_watts", current_value=180.0, new_value=new
            )

    def test_negative_margin_raises(self) -> None:
        with pytest.raises(ValueError, match="margin"):
            propose_threshold_change(
                metric="ftp_watts", current_value=180.0, new_value=195.0, margin=-0.01
            )
