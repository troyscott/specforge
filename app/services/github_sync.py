from __future__ import annotations

import logging

from app.config import get_settings
from app.models.spec import RefinedSpec

logger = logging.getLogger(__name__)


def _section(title: str, body: str) -> str:
    """A Markdown section: an H2 heading followed by its body."""
    return f"## {title}\n\n{body}"


def _text_or_placeholder(value: str) -> str:
    value = value.strip()
    return value if value else "_Not provided._"


def _render_reproduction(steps: list[str]) -> str:
    if not steps:
        return "_No reproduction steps provided._"
    return "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))


def _render_acceptance_criteria(spec: RefinedSpec) -> str:
    if not spec.acceptance_criteria:
        return "_No acceptance criteria._"
    blocks: list[str] = []
    for ac in spec.acceptance_criteria:
        blocks.append(
            f"### {ac.id}\n\n- **Given** {ac.given}\n- **When** {ac.when}\n- **Then** {ac.then}"
        )
    return "\n\n".join(blocks)


def _render_test_plan(spec: RefinedSpec) -> str:
    if not spec.test_plan:
        return "_No test plan items._"
    rows = ["| Criterion | Type | Assertion |", "| --- | --- | --- |"]
    for item in spec.test_plan:
        assertion = item.assertion.replace("|", "\\|")
        rows.append(f"| {item.criterion_id} | {item.test_type.value} | {assertion} |")
    return "\n".join(rows)


def _render_footer(spec: RefinedSpec, signal_url: str) -> str:
    return (
        "---\n\n"
        f"_Synced from [Signal issue #{spec.issue_id}]({signal_url}). "
        "This issue is generated from the refined spec; "
        "edit the spec in Signal, not here._"
    )


def render_issue_body(spec: RefinedSpec, signal_url: str) -> str:
    """Render a GitHub issue body (Markdown) from a refined spec.

    Pure function: no I/O, no network. The single source of truth for the
    issue layout pushed to GitHub.
    """
    title = spec.summary.strip() or f"Refined spec for Signal issue #{spec.issue_id}"
    sections = [
        f"# {title}",
        _section("Context", _text_or_placeholder(spec.context)),
        _section("Expected behavior", _text_or_placeholder(spec.expected_behavior)),
        _section("Actual behavior", _text_or_placeholder(spec.actual_behavior)),
        _section("Reproduction", _render_reproduction(spec.reproduction)),
        _section("Acceptance Criteria", _render_acceptance_criteria(spec)),
        _section("Test Plan", _render_test_plan(spec)),
        _render_footer(spec, signal_url),
    ]
    return "\n\n".join(sections) + "\n"


def sync_issue(spec: RefinedSpec, signal_url: str) -> str:
    """Render the issue body and (R1) log it instead of pushing to GitHub.

    The real GitHub push is gated behind ``settings.github_sync_enabled``
    (default False). In R1 we never make a network call: we render the body
    and log it. When the flag is enabled, the actual push path is still a
    stub so tests never hit the network.

    Returns the rendered issue body regardless of the flag, so callers can
    display or persist it.
    """
    body = render_issue_body(spec, signal_url)
    settings = get_settings()
    if settings.github_sync_enabled:
        # R1: the outbound GitHub API push is intentionally not implemented.
        # WI-8 wires the real push behind this same flag with explicit human
        # approval. We log instead of performing any network call.
        logger.info(
            "github_sync_enabled is True but the GitHub push is stubbed in R1; "
            "rendered issue body for Signal issue #%s (%d chars), not pushed.",
            spec.issue_id,
            len(body),
        )
    else:
        logger.info(
            "github_sync_enabled is False; rendered issue body for Signal issue #%s "
            "(%d chars), not pushed.",
            spec.issue_id,
            len(body),
        )
    return body
