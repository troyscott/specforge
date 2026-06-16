"""AI drafting service for the Refining stage (CLAUDE.md §5).

`draft_spec()` turns a raw issue + project context into a `RefinedSpec`. The
Anthropic client is dependency-injected (a small `RefinerClient` Protocol) so
tests can supply a fake without patching the SDK or hitting the network.

The model is instructed to emit ONLY JSON matching the target schema and to
never invent domain facts: every unverifiable assumption becomes an
`OpenQuestion`. Malformed model output — invalid JSON, schema-invalid JSON, or
a `TestPlanItem.criterion_id` that references no existing criterion — raises a
typed `DraftParseError` at this parse boundary.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from app.config import get_settings
from app.models.spec import (
    AcceptanceCriterion,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
)

__all__ = [
    "DRAFT_SPEC_SYSTEM_PROMPT",
    "DraftParseError",
    "RefinerClient",
    "RefinerError",
    "clarify",
    "draft_spec",
    "draft_test_plan",
    "remove_criterion",
]


class RefinerError(Exception):
    """Base class for all refiner-service failures."""


class DraftParseError(RefinerError):
    """The model's output could not be parsed into a valid RefinedSpec.

    Raised for invalid JSON, JSON that fails schema validation, or a test-plan
    item whose criterion_id references no existing acceptance criterion.
    """


# --- Anthropic client surface (dependency-injected) --------------------------
#
# We depend only on the slice of the SDK we actually call: `messages.create(...)`
# returning an object whose `.content` is a list of blocks, each with `.type`
# and (for text blocks) `.text`. A fake satisfying this Protocol is all a test
# needs — no network, no real API key.


@runtime_checkable
class _ContentBlock(Protocol):
    type: str
    text: str


class _Message(Protocol):
    content: list[Any]


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> _Message: ...  # noqa: ANN401


@runtime_checkable
class RefinerClient(Protocol):
    """Minimal Anthropic-client surface used by the refiner."""

    messages: _Messages


# --- Prompt (CLAUDE.md §5) ----------------------------------------------------

DRAFT_SPEC_SYSTEM_PROMPT = """\
You are the drafting assistant for the Refining stage of Signal. You
turn raw user feedback into an implementation-ready draft spec.

Output rules (follow exactly):
- Output ONLY a single valid JSON object matching the target schema. No prose,
  no explanation, no markdown code fences.
- NEVER invent domain facts — asset names, formulas, business rules, thresholds,
  or anything you cannot verify from the raw feedback. When a fact is needed but
  not supplied, do NOT guess: add an entry to `open_questions` instead.
- Be conservative on scope. Prefer an `out_of_scope` entry over a silent
  assumption.
- Acceptance criteria use Given/When/Then: each item has `id` (AC-1, AC-2, ...),
  `given`, `when`, `then`, and `status` ("draft").
- Open questions use ids Q-1, Q-2, ...; set `resolved` to false and `answer` to
  null until a human resolves them.
- Leave `test_plan` empty at this stage; it is drafted separately.

The JSON object must match this schema (RefinedSpec):
{
  "issue_id": <int>,
  "project_id": <string>,
  "summary": <string>,
  "context": <string>,
  "reproduction": [<string>, ...],
  "expected_behavior": <string>,
  "actual_behavior": <string>,
  "in_scope": [<string>, ...],
  "out_of_scope": [<string>, ...],
  "acceptance_criteria": [
    {"id": <string>, "given": <string>, "when": <string>, "then": <string>,
     "status": "draft"}
  ],
  "test_plan": [],
  "open_questions": [
    {"id": <string>, "question": <string>, "resolved": false, "answer": null}
  ]
}
"""


def _extract_text(message: _Message) -> str:
    """Concatenate the text from a message's text content blocks."""
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        raise DraftParseError("model response contained no text content")
    return "".join(parts)


def _parse_spec(raw_text: str) -> RefinedSpec:
    """Parse model output into a validated RefinedSpec, or raise DraftParseError."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DraftParseError(f"model output was not valid JSON: {exc}") from exc

    try:
        spec = RefinedSpec.model_validate(data)
    except ValidationError as exc:
        raise DraftParseError(f"model output did not match the spec schema: {exc}") from exc

    # CLAUDE.md §4: a test-plan item referencing a non-existent criterion is
    # malformed output. Treat it as a parse failure at this boundary.
    criterion_ids = {c.id for c in spec.acceptance_criteria}
    for item in spec.test_plan:
        if item.criterion_id not in criterion_ids:
            raise DraftParseError(
                f"test_plan item references unknown criterion_id {item.criterion_id!r}"
            )

    return spec


def draft_spec(
    raw_issue: str,
    project_context: str,
    *,
    client: RefinerClient,
) -> RefinedSpec:
    """First-pass AI draft of a RefinedSpec from raw feedback.

    Args:
        raw_issue: The submitter's raw feedback text.
        project_context: Project background to ground the draft.
        client: An injected Anthropic-compatible client (see RefinerClient).

    Returns:
        A validated RefinedSpec.

    Raises:
        DraftParseError: If the model output is not valid, schema-conforming
            JSON, or references a non-existent criterion in the test plan.
    """
    settings = get_settings()

    user_content = (
        f"Project context:\n{project_context}\n\n"
        f"Raw feedback (the submitter's words):\n{raw_issue}\n\n"
        "Draft the RefinedSpec JSON now."
    )

    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=DRAFT_SPEC_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    return _parse_spec(_extract_text(message))


# --- WI-5: test-plan drafting -------------------------------------------------
#
# draft_test_plan walks the spec's acceptance criteria and emits exactly one
# TestPlanItem per criterion. Because each item's criterion_id is read straight
# off the criterion being iterated, every reference is valid BY CONSTRUCTION and
# the test plan covers every criterion (CLAUDE.md §4 referential-integrity seam).
# No Anthropic call is needed: the mapping is total and deterministic, so it is
# both cheaper and trivially network-free in tests. TestType is chosen by a
# conservative heuristic that prefers parity/property/behavioral over manual.


def _choose_test_type(criterion: AcceptanceCriterion) -> TestType:
    """Pick a sensible TestType for a criterion, preferring automatable types.

    Heuristic, in priority order:
    - PARITY    — the criterion is about a computed value matching an expected
      one (equality of totals/amounts/sums). These are the calc-bug cases the
      parity discipline targets (CLAUDE.md §2: exact Decimal assertions).
    - PROPERTY  — the criterion states an invariant that should hold for all/any
      inputs ("always", "never", "for all", "any", "non-negative").
    - BEHAVIORAL — a concrete observable behavior (default for the common case).
    - MANUAL    — only when the criterion concerns inherently non-automatable
      surface (visual/UI/screenshot/look-and-feel).
    """
    haystack = f"{criterion.given} {criterion.when} {criterion.then}".lower()

    manual_markers = ("screenshot", "visual", "looks", "look and feel", "manually", "by eye")
    if any(marker in haystack for marker in manual_markers):
        return TestType.MANUAL

    parity_markers = (
        "equal",
        "equals",
        "matches",
        "match",
        "sum of",
        "total",
        "same as",
        "reconcile",
    )
    if any(marker in haystack for marker in parity_markers):
        return TestType.PARITY

    property_markers = (
        "always",
        "never",
        "for all",
        "for any",
        "any input",
        "every",
        "non-negative",
        "invariant",
        "idempotent",
    )
    if any(marker in haystack for marker in property_markers):
        return TestType.PROPERTY

    return TestType.BEHAVIORAL


def draft_test_plan(spec: RefinedSpec) -> list[TestPlanItem]:
    """Draft one TestPlanItem per acceptance criterion.

    Every produced item references an existing criterion (valid by construction),
    and together the items cover every criterion in the spec — there are no
    dangling references and no uncovered criteria.

    Args:
        spec: The spec whose acceptance criteria to cover.

    Returns:
        A list of TestPlanItems, one per acceptance criterion, in criterion order.
    """
    return [
        TestPlanItem(
            criterion_id=criterion.id,
            assertion=(f"Given {criterion.given}, when {criterion.when}, then {criterion.then}."),
            test_type=_choose_test_type(criterion),
        )
        for criterion in spec.acceptance_criteria
    ]


# --- WI-5: clarification + cascade ----------------------------------------------


def clarify(spec: RefinedSpec, question_id: str, answer: str) -> RefinedSpec:
    """Fold a human's answer to an open question into the spec.

    Marks the matching OpenQuestion resolved and stores the answer. Returns a
    new RefinedSpec (a copy); the input is not mutated.

    Args:
        spec: The spec containing the question.
        question_id: The id (e.g. "Q-1") of the question being answered.
        answer: The human-supplied answer text.

    Returns:
        The updated spec with the question resolved.

    Raises:
        RefinerError: If no open question has the given id.
    """
    updated = spec.model_copy(deep=True)

    for question in updated.open_questions:
        if question.id == question_id:
            question.resolved = True
            question.answer = answer
            return updated

    raise RefinerError(f"no open question with id {question_id!r}")


def remove_criterion(spec: RefinedSpec, criterion_id: str) -> RefinedSpec:
    """Delete an acceptance criterion, cascading to its dependents.

    Removes the criterion plus every TestPlanItem that references it and any
    criterion-scoped OpenQuestion (an OpenQuestion whose id is suffixed with the
    criterion id, e.g. "Q-1:AC-2"). This keeps references from dangling without
    making the cascade a gate condition (CLAUDE.md §4 referential-integrity seam).

    Returns a new RefinedSpec (a copy); the input is not mutated.

    Args:
        spec: The spec to edit.
        criterion_id: The id (e.g. "AC-2") of the criterion to remove.

    Returns:
        The updated spec with the criterion and its dependents removed.

    Raises:
        RefinerError: If no acceptance criterion has the given id.
    """
    if criterion_id not in {c.id for c in spec.acceptance_criteria}:
        raise RefinerError(f"no acceptance criterion with id {criterion_id!r}")

    updated = spec.model_copy(deep=True)

    updated.acceptance_criteria = [c for c in updated.acceptance_criteria if c.id != criterion_id]
    # Cascade: drop test items that pointed at the removed criterion.
    updated.test_plan = [t for t in updated.test_plan if t.criterion_id != criterion_id]
    # Cascade: drop questions scoped to the removed criterion (id "<qid>:<crit>").
    updated.open_questions = [
        q for q in updated.open_questions if _question_scope(q) != criterion_id
    ]

    return updated


def _question_scope(question: OpenQuestion) -> str | None:
    """The criterion id an OpenQuestion is scoped to, or None if unscoped.

    A criterion-scoped question carries the criterion id after a ':' in its id,
    e.g. "Q-3:AC-2" is scoped to "AC-2". Plain ids ("Q-3") are unscoped.
    """
    _, sep, scope = question.id.partition(":")
    return scope if sep else None
