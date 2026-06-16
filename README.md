# SpecForge

SpecForge turns raw user feedback into an implementation-ready spec — a summary, acceptance
criteria, and a test plan — behind a **sync gate**, before anything reaches GitHub.

It's a three-panel refining console:

- **Left** — the raw feedback (read-only): the submitter's words, severity, screenshots.
- **Middle** — the editable draft spec. AI pre-fills; a human edits inline. `NEEDS_INPUT` flags
  mark every assumption the AI could not safely make.
- **Right** — a live sync gate. "Sync to GitHub" stays disabled until every gate condition passes.

The spec produced at the gate is a **contract**: downstream implementation works from the spec,
not from the raw feedback.

## Stack

Python 3.12 · FastAPI · Jinja2 + HTMX · SQLAlchemy 2 (async) + SQLite · Pydantic v2 · Alembic ·
Anthropic API.

## Status

Release 1, in active development. The build is broken into work items tracked as GitHub issues;
see `CLAUDE.md` for the architecture, data model, and build plan.

## Development

### 1. Environment

Uses [micromamba](https://mamba.readthedocs.io/) with an env named `signal` on Python 3.12:

```bash
micromamba create -n signal python=3.12 -y
micromamba activate signal
pip install -e . --group dev          # installs runtime deps + dev tools (pytest, ruff, mypy)
```

Run any tooling without activating the env via `micromamba run -n signal <cmd>`.

### 2. Configuration

Copy the example env file and fill in your values (the real `.env` is gitignored):

```bash
cp .env.example .env
# set ANTHROPIC_API_KEY; DATABASE_URL defaults to sqlite+aiosqlite:///./signal.db
```

### 3. Database + seed data

Create the schema, then load one demo Customer / Project / Issue so the console has data on first
run:

```bash
alembic upgrade head
python scripts/seed.py
```

`scripts/seed.py` is idempotent — safe to re-run; it skips rows that already exist.

### 4. Run

```bash
uvicorn app.main:app --reload --port 8060
```

### 5. Quality gate (run before every PR)

```bash
ruff check . && ruff format --check . && mypy app && pytest -q
```
