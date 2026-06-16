from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.spec import CriterionStatus, RefinedSpec
from app.services.refiner import DraftParseError, RefinerError, draft_spec

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
