"""Seed one Customer / Project / Issue so the console has data on first run.

Run after migrations have created the schema:

    alembic upgrade head
    python scripts/seed.py

The seeding logic lives in ``seed(session)`` so tests can call it directly
against an in-memory database. Re-running is safe: existing rows are left in
place (get-or-create) rather than crashing on duplicate primary keys.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.orm import Customer, Issue, Project

# The single demo record set (Release 1, single user / single tenant).
CUSTOMER_ID = 1
CUSTOMER_NAME = "Acme Co"
PROJECT_ID = "ops-hub"
PROJECT_NAME = "Ops Hub"
ISSUE_ID = 142
ISSUE_TITLE = "Fuel total is wrong on the daily summary"
ISSUE_FEEDBACK = (
    "The daily fuel summary shows a total that doesn't match the sum of the "
    "individual line items. On 2026-06-12 the lines added up to 4,812.50 "
    "gallons but the summary reported 4,810. Looks like the total is being "
    "rounded or truncated somewhere before it's displayed."
)
ISSUE_SEVERITY = "high"


async def seed(session: AsyncSession) -> None:
    """Insert the demo Customer, Project, and Issue if they do not already exist.

    Idempotent: each row is created only when its primary key is absent, so the
    routine can be run repeatedly against the same database without error.
    """
    customer = await session.get(Customer, CUSTOMER_ID)
    if customer is None:
        session.add(Customer(id=CUSTOMER_ID, name=CUSTOMER_NAME))

    project = await session.get(Project, PROJECT_ID)
    if project is None:
        session.add(Project(id=PROJECT_ID, customer_id=CUSTOMER_ID, name=PROJECT_NAME))

    issue = await session.get(Issue, ISSUE_ID)
    if issue is None:
        session.add(
            Issue(
                id=ISSUE_ID,
                project_id=PROJECT_ID,
                title=ISSUE_TITLE,
                raw_feedback=ISSUE_FEEDBACK,
                severity=ISSUE_SEVERITY,
            )
        )

    await session.commit()


async def main() -> None:
    async with SessionLocal() as session:
        await seed(session)
    print(f"Seeded customer {CUSTOMER_NAME!r}, project {PROJECT_ID!r}, issue #{ISSUE_ID}.")


if __name__ == "__main__":
    asyncio.run(main())
