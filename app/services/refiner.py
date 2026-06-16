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
from app.models.spec import RefinedSpec

__all__ = [
    "DRAFT_SPEC_SYSTEM_PROMPT",
    "DraftParseError",
    "RefinerClient",
    "RefinerError",
    "draft_spec",
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
