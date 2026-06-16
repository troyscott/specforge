from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.spec import RefinedSpec


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    projects: Mapped[list[Project]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Project(Base):
    __tablename__ = "project"

    # Project keys are human-readable slugs (e.g. "ops-hub"), matching RefinedSpec.project_id.
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customer.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="projects")
    issues: Mapped[list[Issue]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Issue(Base):
    __tablename__ = "issue"

    # Mirrors RefinedSpec.issue_id (the submitter-facing issue number, e.g. 142).
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    raw_feedback: Mapped[str] = mapped_column(Text, default="", nullable=False)
    severity: Mapped[str] = mapped_column(String(50), default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="issues")
    spec_row: Mapped[RefinedSpecRow | None] = relationship(
        back_populates="issue", cascade="all, delete-orphan", uselist=False
    )


class RefinedSpecRow(Base):
    __tablename__ = "refined_spec"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One refined spec per issue; the queryable mirror of the JSON payload.
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issue.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="refining", nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The full RefinedSpec serialized via pydantic model_dump_json().
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)

    issue: Mapped[Issue] = relationship(back_populates="spec_row")


def _status_for(spec: RefinedSpec) -> str:
    """Derive the queryable status column from the spec's gate state."""
    if spec.is_syncable():
        return "syncable"
    if spec.approved_by is not None:
        return "approved"
    return "refining"


async def get_spec(session: AsyncSession, issue_id: int) -> RefinedSpec | None:
    """Load a RefinedSpec by issue id, or None if no row exists."""
    row = await session.scalar(select(RefinedSpecRow).where(RefinedSpecRow.issue_id == issue_id))
    if row is None:
        return None
    return RefinedSpec.model_validate_json(row.spec_json)


async def save_spec(session: AsyncSession, spec: RefinedSpec) -> None:
    """Upsert a RefinedSpec by issue_id.

    Serializes the full spec into the JSON column and mirrors the queryable
    columns (project_id, status, approved_at). Commits the transaction.
    """
    row = await session.scalar(
        select(RefinedSpecRow).where(RefinedSpecRow.issue_id == spec.issue_id)
    )
    spec_json = spec.model_dump_json()
    if row is None:
        row = RefinedSpecRow(issue_id=spec.issue_id)
        session.add(row)
    row.project_id = spec.project_id
    row.status = _status_for(spec)
    row.approved_at = spec.approved_at
    row.spec_json = spec_json
    await session.commit()


async def list_refining_issues(session: AsyncSession) -> list[RefinedSpec]:
    """Return every spec still in the refining stage (not yet syncable)."""
    rows = await session.scalars(
        select(RefinedSpecRow)
        .where(RefinedSpecRow.status != "syncable")
        .order_by(RefinedSpecRow.issue_id)
    )
    return [RefinedSpec.model_validate_json(r.spec_json) for r in rows]
