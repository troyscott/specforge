from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base
from app.models.orm import (
    Customer,
    Issue,
    Project,
    RefinedSpecRow,
    get_spec,
    list_refining_issues,
    save_spec,
)
from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
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


async def _seed_parents(session: AsyncSession, issue_id: int = 142) -> None:
    session.add(Customer(id=1, name="H&H Oil"))
    session.add(Project(id="ops-hub", customer_id=1, name="Ops Hub"))
    session.add(Issue(id=issue_id, project_id="ops-hub", title="fuel total bug"))
    await session.commit()


def _full_spec(issue_id: int = 142) -> RefinedSpec:
    return RefinedSpec(
        issue_id=issue_id,
        project_id="ops-hub",
        summary="Fix fuel total rounding",
        context="ctx",
        reproduction=["step 1", "step 2"],
        expected_behavior="exact total",
        actual_behavior="rounded total",
        in_scope=["fuel calc"],
        out_of_scope=["ui redesign"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1", given="g1", when="w1", then="t1", status=CriterionStatus.APPROVED
            ),
            AcceptanceCriterion(
                id="AC-2", given="g2", when="w2", then="t2", status=CriterionStatus.APPROVED
            ),
        ],
        test_plan=[
            TestPlanItem(criterion_id="AC-1", assertion="a1", test_type=TestType.PARITY),
            TestPlanItem(criterion_id="AC-2", assertion="a2", test_type=TestType.BEHAVIORAL),
        ],
        open_questions=[OpenQuestion(id="Q-1", question="q", resolved=True, answer="yes")],
        approved_by="troy",
        approved_at=datetime(2026, 6, 15, 12, 30, 45),
    )


async def test_round_trip_equal(session: AsyncSession) -> None:
    await _seed_parents(session)
    spec = _full_spec()
    await save_spec(session, spec)

    loaded = await get_spec(session, 142)
    assert loaded is not None
    assert loaded == spec  # full pydantic equality: nested criteria, test plan, questions, datetime


async def test_get_spec_missing_returns_none(session: AsyncSession) -> None:
    assert await get_spec(session, 999) is None


async def test_save_spec_mirrors_queryable_columns(session: AsyncSession) -> None:
    await _seed_parents(session)
    spec = _full_spec()
    await save_spec(session, spec)

    row = await session.scalar(select(RefinedSpecRow).where(RefinedSpecRow.issue_id == 142))
    assert row is not None
    assert row.project_id == "ops-hub"
    assert row.status == "syncable"  # fully populated approved spec
    assert row.approved_at == datetime(2026, 6, 15, 12, 30, 45)


async def test_save_spec_upsert_by_issue_id(session: AsyncSession) -> None:
    await _seed_parents(session)
    await save_spec(session, _full_spec())

    updated = _full_spec()
    updated.summary = "Revised summary"
    await save_spec(session, updated)

    # Still exactly one row for this issue; content updated.
    rows = (
        await session.scalars(select(RefinedSpecRow).where(RefinedSpecRow.issue_id == 142))
    ).all()
    assert len(rows) == 1
    loaded = await get_spec(session, 142)
    assert loaded is not None
    assert loaded.summary == "Revised summary"


async def test_status_reflects_gate_state(session: AsyncSession) -> None:
    await _seed_parents(session)
    spec = _full_spec()
    spec.approved_by = None  # not approved → not syncable
    await save_spec(session, spec)
    row = await session.scalar(select(RefinedSpecRow).where(RefinedSpecRow.issue_id == 142))
    assert row is not None
    assert row.status == "refining"


async def test_list_refining_issues_excludes_syncable(session: AsyncSession) -> None:
    await _seed_parents(session, issue_id=142)
    session.add(Issue(id=200, project_id="ops-hub", title="other"))
    await session.commit()

    syncable = _full_spec(issue_id=142)
    await save_spec(session, syncable)

    refining = _full_spec(issue_id=200)
    refining.approved_by = None  # keeps it in refining
    await save_spec(session, refining)

    listed = await list_refining_issues(session)
    issue_ids = {s.issue_id for s in listed}
    assert issue_ids == {200}


async def test_fk_enforcement_blocks_orphan_child(session: AsyncSession) -> None:
    # No parent issue exists → inserting a refined_spec must violate the FK.
    session.add(
        RefinedSpecRow(
            issue_id=999,
            project_id="ops-hub",
            status="refining",
            spec_json=_full_spec(999).model_dump_json(),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
