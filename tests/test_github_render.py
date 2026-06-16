from __future__ import annotations

import logging

from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
)
from app.services.github_sync import render_issue_body, sync_issue

SIGNAL_URL = "https://signal.example.com/issues/142"

SECTION_HEADINGS = [
    "## Context",
    "## Expected behavior",
    "## Actual behavior",
    "## Reproduction",
    "## Acceptance Criteria",
    "## Test Plan",
]


def sample_spec() -> RefinedSpec:
    return RefinedSpec(
        issue_id=142,
        project_id="ops-hub",
        summary="Fix fuel total rounding",
        context="Daily fuel totals are off by a cent for H&H Oil.",
        expected_behavior="Totals match the sum of line items to the exact cent.",
        actual_behavior="Totals are rounded per-line, accumulating a cent of drift.",
        reproduction=[
            "Open the ops-hub fuel report for 2026-06-01.",
            "Compare the displayed total to the manual sum.",
        ],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="a report with three line items",
                when="the total is computed",
                then="it equals the Decimal sum to the cent",
                status=CriterionStatus.APPROVED,
            ),
            AcceptanceCriterion(
                id="AC-2",
                given="a report with zero line items",
                when="the total is computed",
                then="it is exactly 0.00",
                status=CriterionStatus.APPROVED,
            ),
        ],
        test_plan=[
            TestPlanItem(
                criterion_id="AC-1",
                assertion="sum of line items == report total",
                test_type=TestType.PARITY,
            ),
            TestPlanItem(
                criterion_id="AC-2",
                assertion="empty report total == Decimal('0.00')",
                test_type=TestType.BEHAVIORAL,
            ),
        ],
        open_questions=[OpenQuestion(id="Q-1", question="q", resolved=True, answer="yes")],
        approved_by="troy",
    )


def test_all_sections_present() -> None:
    body = render_issue_body(sample_spec(), SIGNAL_URL)
    for heading in SECTION_HEADINGS:
        assert heading in body, f"missing section: {heading}"


def test_section_bodies_rendered() -> None:
    spec = sample_spec()
    body = render_issue_body(spec, SIGNAL_URL)
    assert spec.context in body
    assert spec.expected_behavior in body
    assert spec.actual_behavior in body
    for step in spec.reproduction:
        assert step in body


def test_all_acceptance_criteria_appear() -> None:
    spec = sample_spec()
    body = render_issue_body(spec, SIGNAL_URL)
    for ac in spec.acceptance_criteria:
        assert ac.id in body
        assert ac.given in body
        assert ac.when in body
        assert ac.then in body
    # Given/When/Then structure is rendered.
    assert "Given" in body
    assert "When" in body
    assert "Then" in body


def test_all_test_plan_items_appear() -> None:
    spec = sample_spec()
    body = render_issue_body(spec, SIGNAL_URL)
    for item in spec.test_plan:
        assert item.criterion_id in body
        assert item.assertion in body
        assert item.test_type.value in body


def test_footer_links_back_to_signal_url() -> None:
    spec = sample_spec()
    body = render_issue_body(spec, SIGNAL_URL)
    assert SIGNAL_URL in body
    # Rendered as a Markdown link.
    assert f"]({SIGNAL_URL})" in body
    assert f"#{spec.issue_id}" in body


def test_empty_fields_get_placeholders() -> None:
    spec = RefinedSpec(issue_id=7, project_id="ops-hub")
    body = render_issue_body(spec, SIGNAL_URL)
    # All sections still present even with an empty spec.
    for heading in SECTION_HEADINGS:
        assert heading in body
    assert "_Not provided._" in body
    assert "_No reproduction steps provided._" in body
    assert "_No acceptance criteria._" in body
    assert "_No test plan items._" in body


def test_assertion_pipes_are_escaped() -> None:
    spec = sample_spec()
    spec.test_plan = [
        TestPlanItem(
            criterion_id="AC-1",
            assertion="a | b table-breaking pipe",
            test_type=TestType.PARITY,
        )
    ]
    body = render_issue_body(spec, SIGNAL_URL)
    assert "a \\| b table-breaking pipe" in body


def test_sync_issue_returns_body_and_does_not_push_when_disabled(
    caplog: logging.LogCaptureFixture,
) -> None:
    # Default config has github_sync_enabled=False. No network is reachable in tests.
    spec = sample_spec()
    with caplog.at_level(logging.INFO):
        body = sync_issue(spec, SIGNAL_URL)
    assert body == render_issue_body(spec, SIGNAL_URL)
    assert any("not pushed" in rec.message for rec in caplog.records)


def test_sync_issue_does_not_push_even_when_flag_enabled() -> None:
    """Even with the flag on, R1 logs only — the push path is a stub, no network."""
    from app.config import Settings, get_settings
    from app.services import github_sync

    get_settings.cache_clear()
    try:
        # Override the cached settings to simulate the flag being enabled.
        github_sync.get_settings = lambda: Settings(github_sync_enabled=True)  # type: ignore[assignment]
        spec = sample_spec()
        body = sync_issue(spec, SIGNAL_URL)
        assert body == render_issue_body(spec, SIGNAL_URL)
    finally:
        github_sync.get_settings = get_settings  # type: ignore[assignment]
        get_settings.cache_clear()
