from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
)
from app.services.refiner import (
    DraftParseError,
    RefinerError,
    clarify,
    draft_spec,
    draft_test_plan,
    remove_criterion,
)

# --- Fake injected Anthropic client (no network) -----------------------------


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeMessage:
        self.calls.append(kwargs)
        return FakeMessage([FakeTextBlock(self._text)])


class FakeClient:
    """Satisfies the RefinerClient Protocol with a canned text response."""

    def __init__(self, text: str) -> None:
        self.messages = FakeMessages(text)


# --- Canned valid output ------------------------------------------------------


def _valid_spec_json() -> str:
    return json.dumps(
        {
            "issue_id": 142,
            "project_id": "ops-hub",
            "summary": "Fuel total is computed incorrectly on the daily report",
            "context": "Reported by the operations team.",
            "reproduction": ["Open the daily report", "Compare the fuel total to line items"],
            "expected_behavior": "The fuel total equals the sum of the line items.",
            "actual_behavior": "The fuel total is lower than the sum of the line items.",
            "in_scope": ["Daily report fuel total calculation"],
            "out_of_scope": ["Historical report backfill"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "a daily report with fuel line items",
                    "when": "the report is generated",
                    "then": "the fuel total equals the sum of the line items",
                    "status": "draft",
                }
            ],
            "test_plan": [],
            "open_questions": [
                {
                    "id": "Q-1",
                    "question": "What rounding rule applies to the fuel total?",
                    "resolved": False,
                    "answer": None,
                }
            ],
        }
    )


def test_draft_spec_produces_valid_refined_spec() -> None:
    client = FakeClient(_valid_spec_json())

    spec = draft_spec("Fuel total looks wrong", "ops-hub project", client=client)

    assert isinstance(spec, RefinedSpec)
    assert spec.issue_id == 142
    assert spec.project_id == "ops-hub"
    assert len(spec.acceptance_criteria) == 1
    assert spec.acceptance_criteria[0].status == CriterionStatus.DRAFT
    # The injected client received the configured model and a user message.
    create_kwargs = client.messages.calls[0]
    assert create_kwargs["model"] == "claude-opus-4-8"
    assert create_kwargs["messages"][0]["role"] == "user"


def test_draft_spec_preserves_open_question() -> None:
    client = FakeClient(_valid_spec_json())

    spec = draft_spec("Fuel total looks wrong", "ops-hub project", client=client)

    assert len(spec.open_questions) >= 1
    q = spec.open_questions[0]
    assert q.id == "Q-1"
    assert q.resolved is False
    assert q.answer is None


def test_draft_spec_raises_on_malformed_json() -> None:
    client = FakeClient("this is not JSON, just prose")

    with pytest.raises(DraftParseError):
        draft_spec("Fuel total looks wrong", "ops-hub project", client=client)


def test_draft_spec_raises_on_schema_invalid_json() -> None:
    # Valid JSON, but missing required fields (issue_id, project_id).
    client = FakeClient(json.dumps({"summary": "x"}))

    with pytest.raises(DraftParseError):
        draft_spec("Fuel total looks wrong", "ops-hub project", client=client)


def test_draft_spec_raises_on_dangling_criterion_id() -> None:
    payload = json.loads(_valid_spec_json())
    payload["test_plan"] = [
        {"criterion_id": "AC-999", "assertion": "totals match", "test_type": "parity"}
    ]
    client = FakeClient(json.dumps(payload))

    with pytest.raises(DraftParseError):
        draft_spec("Fuel total looks wrong", "ops-hub project", client=client)


def test_draft_parse_error_is_a_refiner_error() -> None:
    # DraftParseError is the typed error; it subclasses RefinerError.
    assert issubclass(DraftParseError, RefinerError)


def test_draft_spec_accepts_matching_test_plan_item() -> None:
    # A test_plan item that DOES reference an existing criterion parses fine.
    payload = json.loads(_valid_spec_json())
    payload["test_plan"] = [
        {"criterion_id": "AC-1", "assertion": "totals match", "test_type": "parity"}
    ]
    client = FakeClient(json.dumps(payload))

    spec = draft_spec("Fuel total looks wrong", "ops-hub project", client=client)
    assert len(spec.test_plan) == 1
    assert spec.test_plan[0].criterion_id == "AC-1"


# --- WI-5: draft_test_plan ----------------------------------------------------


def _ac(
    id_: str,
    then: str = "the result is correct",
    *,
    when: str = "it runs",
) -> AcceptanceCriterion:
    return AcceptanceCriterion(id=id_, given="a precondition", when=when, then=then)


def _spec_with(
    criteria: list[AcceptanceCriterion],
    *,
    test_plan: list[TestPlanItem] | None = None,
    open_questions: list[OpenQuestion] | None = None,
) -> RefinedSpec:
    return RefinedSpec(
        issue_id=142,
        project_id="ops-hub",
        summary="A summary",
        acceptance_criteria=criteria,
        test_plan=test_plan or [],
        open_questions=open_questions or [],
    )


def test_draft_test_plan_covers_every_criterion() -> None:
    # AC: every criterion id is referenced by >=1 produced TestPlanItem.
    spec = _spec_with([_ac("AC-1"), _ac("AC-2"), _ac("AC-3")])

    plan = draft_test_plan(spec)

    referenced = {item.criterion_id for item in plan}
    criterion_ids = {c.id for c in spec.acceptance_criteria}
    assert referenced == criterion_ids
    # One item per criterion.
    assert len(plan) == len(spec.acceptance_criteria)


def test_draft_test_plan_refs_are_all_valid() -> None:
    # AC: produced refs are all valid (no dangling references by construction).
    spec = _spec_with([_ac("AC-1"), _ac("AC-2")])

    plan = draft_test_plan(spec)

    criterion_ids = {c.id for c in spec.acceptance_criteria}
    assert all(item.criterion_id in criterion_ids for item in plan)


def test_draft_test_plan_empty_when_no_criteria() -> None:
    spec = _spec_with([])
    assert draft_test_plan(spec) == []


def test_draft_test_plan_prefers_automatable_test_types() -> None:
    # Parity for a totals/equality criterion; behavioral for a plain one;
    # property for an invariant; manual only for visual criteria.
    spec = _spec_with(
        [
            _ac("AC-1", then="the fuel total equals the sum of the line items"),
            _ac("AC-2", then="the user is shown a confirmation banner"),
            _ac("AC-3", then="the balance is never negative for any input"),
            _ac("AC-4", then="the chart looks correct", when="reviewed by screenshot"),
        ]
    )

    by_id = {item.criterion_id: item for item in draft_test_plan(spec)}

    assert by_id["AC-1"].test_type == TestType.PARITY
    assert by_id["AC-2"].test_type == TestType.BEHAVIORAL
    assert by_id["AC-3"].test_type == TestType.PROPERTY
    assert by_id["AC-4"].test_type == TestType.MANUAL


# --- WI-5: clarify ------------------------------------------------------------


def test_clarify_resolves_question_and_stores_answer() -> None:
    # AC: clarify flips resolved=True and stores the answer.
    spec = _spec_with(
        [_ac("AC-1")],
        open_questions=[
            OpenQuestion(id="Q-1", question="What rounding rule?"),
            OpenQuestion(id="Q-2", question="Which timezone?"),
        ],
    )

    updated = clarify(spec, "Q-1", "Round half-up to two decimals")

    q1 = next(q for q in updated.open_questions if q.id == "Q-1")
    assert q1.resolved is True
    assert q1.answer == "Round half-up to two decimals"
    # The other question is untouched.
    q2 = next(q for q in updated.open_questions if q.id == "Q-2")
    assert q2.resolved is False
    # Input spec is not mutated.
    assert spec.open_questions[0].resolved is False


def test_clarify_resolving_last_question_makes_all_resolved() -> None:
    # AC: resolving the LAST open question makes all(q.resolved) true.
    spec = _spec_with(
        [_ac("AC-1")],
        open_questions=[
            OpenQuestion(id="Q-1", question="First?", resolved=True, answer="yes"),
            OpenQuestion(id="Q-2", question="Last?"),
        ],
    )

    assert not all(q.resolved for q in spec.open_questions)

    updated = clarify(spec, "Q-2", "resolved now")

    assert all(q.resolved for q in updated.open_questions)


def test_clarify_unknown_question_raises() -> None:
    spec = _spec_with([_ac("AC-1")], open_questions=[OpenQuestion(id="Q-1", question="?")])

    with pytest.raises(RefinerError):
        clarify(spec, "Q-999", "no such question")


# --- WI-5: remove_criterion (cascade) -----------------------------------------


def test_remove_criterion_drops_its_test_items() -> None:
    # AC: deleting a criterion removes its test items; no dangling refs remain.
    spec = _spec_with(
        [_ac("AC-1"), _ac("AC-2")],
        test_plan=[
            TestPlanItem(criterion_id="AC-1", assertion="a", test_type=TestType.BEHAVIORAL),
            TestPlanItem(criterion_id="AC-2", assertion="b", test_type=TestType.BEHAVIORAL),
            TestPlanItem(criterion_id="AC-2", assertion="c", test_type=TestType.PARITY),
        ],
    )

    updated = remove_criterion(spec, "AC-2")

    remaining_criteria = {c.id for c in updated.acceptance_criteria}
    assert remaining_criteria == {"AC-1"}
    # No test item references the removed criterion.
    assert all(t.criterion_id != "AC-2" for t in updated.test_plan)
    # No test item dangles (every ref points at a surviving criterion).
    assert all(t.criterion_id in remaining_criteria for t in updated.test_plan)
    # Input spec is not mutated.
    assert {c.id for c in spec.acceptance_criteria} == {"AC-1", "AC-2"}


def test_remove_criterion_drops_scoped_open_questions() -> None:
    # A criterion-scoped question ("Q-3:AC-2") is dropped; unscoped ones survive.
    spec = _spec_with(
        [_ac("AC-1"), _ac("AC-2")],
        open_questions=[
            OpenQuestion(id="Q-1", question="unscoped"),
            OpenQuestion(id="Q-3:AC-2", question="scoped to AC-2"),
        ],
    )

    updated = remove_criterion(spec, "AC-2")

    remaining_question_ids = {q.id for q in updated.open_questions}
    assert remaining_question_ids == {"Q-1"}


def test_remove_criterion_unknown_id_raises() -> None:
    spec = _spec_with([_ac("AC-1")])

    with pytest.raises(RefinerError):
        remove_criterion(spec, "AC-999")
