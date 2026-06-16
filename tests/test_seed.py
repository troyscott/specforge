from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base
from app.models.orm import Customer, Issue, Project, list_refining_issues, save_spec
from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    RefinedSpec,
)
from scripts.seed import (
    CUSTOMER_NAME,
    ISSUE_ID,
    PROJECT_ID,
    seed,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with PRAGMA foreign_keys=ON (mirrors app/db.py)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_seed_inserts_demo_rows(session: AsyncSession) -> None:
    await seed(session)

    customer = await session.scalar(select(Customer).where(Customer.name == CUSTOMER_NAME))
    assert customer is not None

    project = await session.get(Project, PROJECT_ID)
    assert project is not None
    assert project.customer_id == customer.id

    issue = await session.get(Issue, ISSUE_ID)
    assert issue is not None
    assert issue.project_id == PROJECT_ID
    assert issue.raw_feedback  # has feedback text
    assert "fuel" in issue.title.lower()


async def test_seeded_issue_appears_in_refining_list(session: AsyncSession) -> None:
    await seed(session)

    # A fresh, un-approved spec for the seeded issue stays in the refining stage.
    spec = RefinedSpec(
        issue_id=ISSUE_ID,
        project_id=PROJECT_ID,
        summary="Fuel total mismatch",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1", given="g", when="w", then="t", status=CriterionStatus.DRAFT
            )
        ],
    )
    await save_spec(session, spec)

    listed = await list_refining_issues(session)
    assert ISSUE_ID in {s.issue_id for s in listed}


async def test_seed_is_idempotent(session: AsyncSession) -> None:
    await seed(session)
    await seed(session)  # must not raise on duplicate primary keys

    customers = (await session.scalars(select(Customer))).all()
    projects = (await session.scalars(select(Project))).all()
    issues = (await session.scalars(select(Issue))).all()
    assert len(customers) == 1
    assert len(projects) == 1
    assert len(issues) == 1
