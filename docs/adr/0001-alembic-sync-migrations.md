# ADR 0001 — Alembic runs synchronously against SQLite

- **Status:** Accepted
- **Date:** 2026-06-15
- **Context:** WI-0 (scaffold)

## Context

The application runs async on SQLAlchemy 2.x with the `aiosqlite` driver
(`sqlite+aiosqlite:///./signal.db`). Alembic, however, executes migrations through a normal
synchronous flow. We need one consistent migration setup that every later work item (notably WI-2)
follows.

## Decision

Migrations run **synchronously**. `app/config.py` exposes `Settings.sync_database_url`, which
strips the `+aiosqlite` driver suffix to yield `sqlite:///./signal.db`. `migrations/env.py` sets
this as the Alembic `sqlalchemy.url` at runtime, so the app keeps its async URL while Alembic uses
the sync one. The async engine and `env.py` are the only two places that touch a database URL.

Batch mode (`render_as_batch=True`) is enabled in `env.py` because SQLite cannot `ALTER TABLE` in
place; Alembic emits copy-and-rename batches instead.

## Consequences

- No async `env.py` plumbing; standard Alembic templates work unchanged.
- WI-2 writes ordinary synchronous migrations and imports models onto `Base.metadata` (env.py
  already imports `app.models.orm` tolerantly).
- A single source for the URL: change `DATABASE_URL`, and both the app and migrations follow.
