from __future__ import annotations

from collections.abc import Callable

import pytest

from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
)

EXPECTED_LABELS = [
    "Summary set",
    "Criteria ≥ 1",
    "All criteria approved",
    "Test plan covers criteria",
    "No open questions",
    "Approved",
]


def approved_spec() -> RefinedSpec:
    """A fully populated spec that passes every gate condition."""
    return RefinedSpec(
        issue_id=142,
        project_id="ops-hub",
        summary="Fix fuel total rounding",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1", given="g", when="w", then="t", status=CriterionStatus.APPROVED
            ),
            AcceptanceCriterion(
                id="AC-2", given="g", when="w", then="t", status=CriterionStatus.APPROVED
            ),
        ],
        test_plan=[
            TestPlanItem(criterion_id="AC-1", assertion="a1", test_type=TestType.PARITY),
            TestPlanItem(criterion_id="AC-2", assertion="a2", test_type=TestType.BEHAVIORAL),
        ],
        open_questions=[OpenQuestion(id="Q-1", question="q", resolved=True, answer="yes")],
        approved_by="troy",
    )


def test_fully_populated_spec_is_syncable() -> None:
    assert approved_spec().is_syncable() is True


def test_gate_items_returns_six_ordered_pairs() -> None:
    items = approved_spec().gate_items()
    assert len(items) == 6
    assert [label for label, _ in items] == EXPECTED_LABELS
    assert all(passing for _, passing in items)


# Each entry: a mutation that breaks exactly one gate condition, and the label it must flip.
BREAKERS: dict[str, tuple[Callable[[RefinedSpec], None], str]] = {
    "summary": (lambda s: setattr(s, "summary", "  "), "Summary set"),
    "no_criteria": (
        lambda s: (setattr(s, "acceptance_criteria", []), setattr(s, "test_plan", []))[0],
        "Criteria ≥ 1",
    ),
    "criterion_not_approved": (
        lambda s: setattr(s.acceptance_criteria[0], "status", CriterionStatus.DRAFT),
        "All criteria approved",
    ),
    "test_plan_missing_coverage": (
        lambda s: setattr(s, "test_plan", [s.test_plan[0]]),  # only AC-1 covered
        "Test plan covers criteria",
    ),
    "open_question_unresolved": (
        lambda s: setattr(s.open_questions[0], "resolved", False),
        "No open questions",
    ),
    "not_approved": (lambda s: setattr(s, "approved_by", None), "Approved"),
}


@pytest.mark.parametrize("name", list(BREAKERS))
def test_breaking_any_single_condition_blocks_sync(name: str) -> None:
    mutate, label = BREAKERS[name]
    spec = approved_spec()
    mutate(spec)
    assert spec.is_syncable() is False
    flipped = {lbl: ok for lbl, ok in spec.gate_items()}
    assert flipped[label] is False


def test_coverage_is_per_criterion_not_count() -> None:
    """Two criteria with a test plan that references only one → gate fails.

    Regression for the old `len(test_plan) >= len(criteria)` check, which would
    have passed this (two items, but both could point at the same criterion).
    """
    spec = approved_spec()
    # Two test items, but both reference AC-1 — AC-2 is uncovered.
    spec.test_plan = [
        TestPlanItem(criterion_id="AC-1", assertion="a1", test_type=TestType.PARITY),
        TestPlanItem(criterion_id="AC-1", assertion="a1b", test_type=TestType.PROPERTY),
    ]
    assert len(spec.test_plan) >= len(spec.acceptance_criteria)  # count check would pass
    assert spec.is_syncable() is False  # coverage check correctly fails
    flipped = {lbl: ok for lbl, ok in spec.gate_items()}
    assert flipped["Test plan covers criteria"] is False
