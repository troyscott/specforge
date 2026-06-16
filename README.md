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

A full dev runbook (environment setup, seeding, running, and tests) lands with the work item that
covers the seed/config/runbook. Until then, `CLAUDE.md` § 9 lists the commands.
