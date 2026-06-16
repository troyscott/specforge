from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app
from app.models.orm import Customer, Issue, Project, save_spec
from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
    TestPlanItem,
    TestType,
)


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite bound to a single connection (shared across sessions)."""
    # StaticPool keeps a single connection so every session sees the same
    # in-memory database (the route's session and the test's setup session).
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def routed_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """AsyncClient whose app uses the in-memory DB via a dependency override."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_session, None)


async def _seed_parents(maker: async_sessionmaker[AsyncSession], issue_id: int = 142) -> None:
    async with maker() as s:
        s.add(Customer(id=1, name="Acme Co"))
        s.add(Project(id="ops-hub", customer_id=1, name="Ops Hub"))
        s.add(Issue(id=issue_id, project_id="ops-hub", title="fuel total bug"))
        await s.commit()


def _non_syncable_spec(issue_id: int = 142) -> RefinedSpec:
    """Approved-but-not-yet-syncable: an open question keeps the gate closed."""
    return RefinedSpec(
        issue_id=issue_id,
        project_id="ops-hub",
        summary="",  # summary not set yet → "Summary set" fails
        context="Totals are off by a cent on multi-line fuel orders.",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1", given="g1", when="w1", then="t1", status=CriterionStatus.DRAFT
            ),
        ],
        open_questions=[
            OpenQuestion(id="Q-1", question="Which rounding rule applies?", resolved=False),
        ],
    )


def _syncable_spec(issue_id: int = 142) -> RefinedSpec:
    """A fully-populated, approved spec where every gate item passes."""
    from datetime import datetime

    return RefinedSpec(
        issue_id=issue_id,
        project_id="ops-hub",
        summary="Fix fuel total rounding",
        context="ctx",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1", given="g1", when="w1", then="t1", status=CriterionStatus.APPROVED
            ),
        ],
        test_plan=[
            TestPlanItem(criterion_id="AC-1", assertion="a1", test_type=TestType.PARITY),
        ],
        open_questions=[],
        approved_by="reviewer",
        approved_at=datetime(2026, 6, 15, 12, 0, 0),
    )


# --- AC: console renders for a seeded issue -------------------------------------


async def test_console_renders_three_panels(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_parents(db_sessionmaker)
    async with db_sessionmaker() as s:
        await save_spec(s, _non_syncable_spec())

    resp = await routed_client.get("/refining/142")
    assert resp.status_code == 200
    body = resp.text
    # Three panels + their key content.
    assert "Raw feedback" in body
    assert "Draft spec" in body
    assert "Sync gate" in body
    assert "Totals are off by a cent" in body  # left panel raw context
    assert "Which rounding rule applies?" in body  # needs-input flag
    assert "Sync to GitHub" in body  # gate panel button


async def test_console_missing_issue_404(routed_client: AsyncClient) -> None:
    resp = await routed_client.get("/refining/999")
    assert resp.status_code == 404


# --- AC: PATCH summary returns gate partial with "Summary set" passing ----------


async def test_patch_summary_returns_gate_partial_with_summary_passing(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_parents(db_sessionmaker)
    async with db_sessionmaker() as s:
        await save_spec(s, _non_syncable_spec())  # summary empty → fails initially

    resp = await routed_client.patch(
        "/refining/142/summary", data={"summary": "Fix fuel total rounding"}
    )
    assert resp.status_code == 200
    body = resp.text
    # It is the gate panel partial (not a full page).
    assert 'id="gate-panel"' in body
    assert "<html" not in body.lower()
    # "Summary set" now passes: it sits in a passing gate item.
    assert "Summary set" in body
    assert "is-passing" in body

    # Persisted through the repo.
    from app.models.orm import get_spec

    async with db_sessionmaker() as s:
        reloaded = await get_spec(s, 142)
    assert reloaded is not None
    assert reloaded.summary == "Fix fuel total rounding"


# --- AC: sync button disabled while gate not clear, enabled when syncable -------


async def test_sync_button_disabled_when_not_syncable(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_parents(db_sessionmaker)
    async with db_sessionmaker() as s:
        spec = _non_syncable_spec()
        assert spec.is_syncable() is False
        await save_spec(s, spec)

    resp = await routed_client.get("/refining/142")
    assert resp.status_code == 200
    # The button carries the disabled attribute.
    assert "btn-sync" in resp.text
    assert "disabled" in resp.text


async def test_sync_button_enabled_when_syncable(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_parents(db_sessionmaker)
    async with db_sessionmaker() as s:
        spec = _syncable_spec()
        assert spec.is_syncable() is True
        await save_spec(s, spec)

    resp = await routed_client.get("/refining/142")
    assert resp.status_code == 200
    body = resp.text
    # Isolate the sync button markup and assert it is NOT disabled.
    start = body.index('class="btn btn-sync"')
    end = body.index(">", start)
    button_open_tag = body[start:end]
    assert "disabled" not in button_open_tag


@pytest.mark.parametrize("issue_id", [142])
async def test_patch_missing_issue_404(routed_client: AsyncClient, issue_id: int) -> None:
    resp = await routed_client.patch(f"/refining/{issue_id}/summary", data={"summary": "x"})
    assert resp.status_code == 404
