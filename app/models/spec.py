from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CriterionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class TestType(str, Enum):
    PARITY = "parity"
    PROPERTY = "property"
    BEHAVIORAL = "behavioral"
    MANUAL = "manual"


class AcceptanceCriterion(BaseModel):
    id: str  # AC-1, AC-2 ...
    given: str
    when: str
    then: str
    status: CriterionStatus = CriterionStatus.DRAFT


class TestPlanItem(BaseModel):
    criterion_id: str  # references an existing AcceptanceCriterion.id
    assertion: str
    test_type: TestType


class OpenQuestion(BaseModel):
    id: str  # Q-1, Q-2 ...
    question: str
    resolved: bool = False
    answer: str | None = None


class RefinedSpec(BaseModel):
    issue_id: int
    project_id: str
    summary: str = ""
    context: str = ""
    reproduction: list[str] = Field(default_factory=list)
    expected_behavior: str = ""
    actual_behavior: str = ""
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    test_plan: list[TestPlanItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    drafted_by: str = "ai"
    approved_by: str | None = None
    approved_at: datetime | None = None

    def gate_items(self) -> list[tuple[str, bool]]:
        """Ordered (label, passing) pairs for the gate panel. UI renders this verbatim.

        This is the SINGLE source of truth for the gate; is_syncable() is derived
        from it (see below). Never re-implement these conditions anywhere else.
        """
        covered = {t.criterion_id for t in self.test_plan}
        return [
            ("Summary set", bool(self.summary.strip())),
            ("Criteria ≥ 1", len(self.acceptance_criteria) >= 1),
            (
                "All criteria approved",
                len(self.acceptance_criteria) >= 1
                and all(c.status == CriterionStatus.APPROVED for c in self.acceptance_criteria),
            ),
            (
                "Test plan covers criteria",
                len(self.acceptance_criteria) >= 1
                and all(c.id in covered for c in self.acceptance_criteria),
            ),
            ("No open questions", all(q.resolved for q in self.open_questions)),
            ("Approved", self.approved_by is not None),
        ]

    def is_syncable(self) -> bool:
        # Derived from gate_items() so the rule lives in exactly one place.
        return all(passing for _, passing in self.gate_items())
