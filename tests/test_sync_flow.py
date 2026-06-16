"""WI-8 — end-to-end route flow + server-side sync gate.

Drives the refining router through the full lifecycle with an in-memory DB and a
fake refiner client (dependency-overridden, so NO network is hit), and asserts
the server-side sync gate rejects forced syncs on a non-syncable spec.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app
from app.models.orm import Customer, Issue, Project, get_spec, save_spec
from app.models.spec import (
    AcceptanceCriterion,
    CriterionStatus,
    OpenQuestion,
    RefinedSpec,
)
from app.routes.refining import get_refiner_client

# --- Fake injected Anthropic client (no network) -----------------------------


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return _FakeMessage([_FakeBlock(self._text)])


class FakeClient:
    """Satisfies refiner.RefinerClient with a canned text response. No network."""

    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def _draft_json() -> str:
    """One acceptance criterion, one open question — a realistic first draft."""
    return json.dumps(
        {
            "issue_id": 142,
            "project_id": "ops-hub",
            "summary": "Fix fuel total computed incorrectly on the daily report",
            "context": "Reported by the operations team.",
            "reproduction": ["Open the daily report", "Compare total to line items"],
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


# --- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def db_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
async def fake_client() -> FakeClient:
    return FakeClient(_draft_json())


@pytest_asyncio.fixture
async def routed_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_client: FakeClient,
) -> AsyncIterator[AsyncClient]:
    """AsyncClient with the in-memory DB and a FAKE refiner client — no network."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_refiner_client] = lambda: fake_client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_refiner_client, None)


async def _seed(maker: async_sessionmaker[AsyncSession], issue_id: int = 142) -> None:
    async with maker() as s:
        s.add(Customer(id=1, name="Acme Co"))
        s.add(Project(id="ops-hub", customer_id=1, name="Ops Hub"))
        s.add(Issue(id=issue_id, project_id="ops-hub", title="fuel total bug"))
        await s.commit()
        # Seed a bare spec so the console/draft has a row to work from.
        await save_spec(
            s,
            RefinedSpec(
                issue_id=issue_id,
                project_id="ops-hub",
                context="Fuel total doesn't match the sum of the line items.",
            ),
        )


# --- AC: full flow seed → draft → resolve → approve → test plan → approve → sync


async def test_full_flow_to_sync(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_sessionmaker)

    # 1) draft (fake client) — populates summary, criterion, open question.
    resp = await routed_client.post("/refining/142/draft")
    assert resp.status_code == 200
    async with db_sessionmaker() as s:
        spec = await get_spec(s, 142)
    assert spec is not None
    assert spec.summary
    assert len(spec.acceptance_criteria) == 1
    assert spec.acceptance_criteria[0].status == CriterionStatus.DRAFT
    assert any(not q.resolved for q in spec.open_questions)
    assert spec.is_syncable() is False

    # 2) answer the open question (resolve).
    resp = await routed_client.post(
        "/refining/142/resolve-question",
        data={"question_id": "Q-1", "answer": "Round half-up to the cent."},
    )
    assert resp.status_code == 200
    async with db_sessionmaker() as s:
        spec = await get_spec(s, 142)
    assert spec is not None
    assert all(q.resolved for q in spec.open_questions)
    assert spec.open_questions[0].answer == "Round half-up to the cent."

    # 3) draft the test plan (one item per criterion).
    resp = await routed_client.post("/refining/142/draft-test-plan")
    assert resp.status_code == 200
    async with db_sessionmaker() as s:
        spec = await get_spec(s, 142)
    assert spec is not None
    assert len(spec.test_plan) >= len(spec.acceptance_criteria)

    # 4) approve — criteria → APPROVED and approved_by set; gate goes green.
    resp = await routed_client.post("/refining/142/approve", data={"approved_by": "troy"})
    assert resp.status_code == 200
    body = resp.text
    assert 'id="gate-panel"' in body  # mutating route returns the gate partial
    async with db_sessionmaker() as s:
        spec = await get_spec(s, 142)
    assert spec is not None
    assert all(c.status == CriterionStatus.APPROVED for c in spec.acceptance_criteria)
    assert spec.approved_by == "troy"
    assert spec.is_syncable() is True

    # 5) sync — gate green → renders the issue body with all sections.
    resp = await routed_client.post("/refining/142/sync")
    assert resp.status_code == 200
    body = resp.text
    assert "Context" in body
    assert "Expected behavior" in body
    assert "Actual behavior" in body
    assert "Reproduction" in body
    assert "Acceptance Criteria" in body
    assert "Test Plan" in body
    assert "AC-1" in body
    # Footer links back to the Signal console URL for this issue.
    assert "/refining/142" in body


# --- AC: server REJECTS a forced sync when not syncable -----------------------


async def test_sync_rejected_when_not_syncable(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_sessionmaker)
    # A spec that explicitly fails the gate (draft criterion + open question).
    async with db_sessionmaker() as s:
        spec = RefinedSpec(
            issue_id=142,
            project_id="ops-hub",
            summary="",  # summary not set → gate fails
            context="ctx",
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1", given="g", when="w", then="t", status=CriterionStatus.DRAFT
                ),
            ],
            open_questions=[OpenQuestion(id="Q-1", question="?", resolved=False)],
        )
        assert spec.is_syncable() is False
        await save_spec(s, spec)

    # Forcing the request (the button would be disabled client-side) is rejected.
    resp = await routed_client.post("/refining/142/sync")
    assert resp.status_code == 409
    body = resp.text
    # No issue body is produced — none of the rendered sections appear.
    assert "Acceptance Criteria" not in body
    assert "Test Plan" not in body
    assert "synced issue" not in body.lower()


# --- AC: no network — draft works purely off the injected fake client ---------


async def test_draft_uses_injected_fake_client_no_network(
    routed_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_client: FakeClient,
) -> None:
    await _seed(db_sessionmaker)
    resp = await routed_client.post("/refining/142/draft")
    assert resp.status_code == 200
    # The fake client's messages.create was invoked with the configured model.
    assert len(fake_client.messages.calls) == 1
    assert fake_client.messages.calls[0]["model"] == "claude-opus-4-8"


async def test_resolve_unknown_question_404(
    routed_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(db_sessionmaker)
    resp = await routed_client.post(
        "/refining/142/resolve-question",
        data={"question_id": "Q-404", "answer": "x"},
    )
    assert resp.status_code == 404
