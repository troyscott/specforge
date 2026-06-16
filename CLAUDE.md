# CLAUDE.md — Signal Refining Stage (Release 1)

> Operating manual for Claude Code sessions building the **Refining stage** of Persepta Signal:
> the layer that turns raw user feedback into an implementation-ready spec (summary + acceptance
> criteria + test plan) before anything syncs to GitHub.
>
> **Release 1 scope:** single user (Troy), single-tenant-capable but multi-tenant-aware schema,
> **SQLite** backend, FastAPI + HTMX, AI drafting via the Anthropic API. No Postgres/Azure SQL in R1.

---

## 1. What we are building

The Refining stage slots into the existing Signal pipeline:

```
Submit issue → Triage → [REFINING] → (gate) → GitHub issue → PR → status sync back
```

The Refining stage is a three-panel console:

- **Left** — raw feedback (read-only; the submitter's words, severity, screenshots).
- **Middle** — the editable draft spec. AI pre-fills; the human edits inline. `NEEDS_INPUT`
  flags mark every domain assumption the AI could not safely make.
- **Right** — the live **sync gate**: a computed checklist off `RefinedSpec.is_syncable()`.
  The "Sync to GitHub" action is disabled until every gate condition passes.

The spec produced at the gate is a **contract**. Once it exists, downstream implementation works
from the spec — not from the raw feedback. This is the firewall: raw feedback enters the Refining
room and does not leak past the gate.

---

## 2. Stack & conventions (Release 1)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | type hints everywhere; `from __future__ import annotations` |
| Web | FastAPI | async routes |
| Templating | Jinja2 + HTMX | server-rendered partials; no SPA, no React |
| DB | **SQLite** | via SQLAlchemy 2.x (async) + `aiosqlite`; one file `signal.db` |
| Migrations | Alembic | even on SQLite — R2 swaps the URL, not the migration history |
| Models | Pydantic v2 | API/spec schemas; SQLAlchemy models are separate ORM layer |
| AI | `anthropic` SDK | model `claude-opus-4-8` for drafting; key from env, never hardcoded |
| Tests | pytest + pytest-asyncio | + `httpx.AsyncClient` for route tests |
| Lint/format | ruff | `ruff check` and `ruff format`; run before every PR |
| Types | mypy | strict on `app/`, lenient on `tests/` |
| Env mgmt | micromamba | env name `signal`; Python 3.12 |

**Hard rules:**
- No secrets in code. `ANTHROPIC_API_KEY` and all config come from environment / `.env` (gitignored).
- SQLite-specific: enable `PRAGMA foreign_keys=ON` on every connection. Use `aiosqlite`.
- Money/quantity math is `Decimal`, never `float`. (Carried over from the parity discipline —
  acceptance criteria for calc bugs must assert exact values.)
- Every route that mutates returns an HTMX partial, not a full page.
- The gate logic (`is_syncable()`) lives in **one** place (the Pydantic model). The UI reads it;
  it never re-implements the rule.

---

## 3. Repository layout

```
signal/                        # repo root (working dir)
├── CLAUDE.md                  # this file
├── pyproject.toml             # ruff, mypy, pytest config
├── alembic.ini
├── .env.example               # documents required env vars
├── app/
│   ├── main.py                # FastAPI app factory, router registration
│   ├── config.py              # pydantic-settings; reads env
│   ├── db.py                  # async engine, session, PRAGMA foreign_keys
│   ├── models/
│   │   ├── orm.py             # SQLAlchemy tables: Issue, RefinedSpecRow, Project, Customer
│   │   └── spec.py            # Pydantic: RefinedSpec, AcceptanceCriterion, TestPlanItem
│   ├── services/
│   │   ├── refiner.py         # Anthropic drafting: draft_spec(), draft_test_plan(), clarify()
│   │   └── github_sync.py     # render_issue_body(); R1 stubs the actual API push behind a flag
│   ├── routes/
│   │   ├── refining.py        # GET console, PATCH spec fields, POST gate-eval, POST sync
│   │   └── partials.py        # HTMX partial renderers (gate panel, NEEDS_INPUT list)
│   └── templates/
│       ├── base.html
│       ├── refining_console.html
│       └── partials/
│           ├── gate_panel.html
│           ├── spec_form.html
│           └── needs_input.html
├── tests/
│   ├── conftest.py            # in-memory SQLite fixture, AsyncClient fixture
│   ├── test_spec_model.py     # is_syncable() truth table
│   ├── test_refiner.py        # mocked Anthropic responses
│   ├── test_routes.py         # console render, PATCH, gate eval
│   └── test_github_render.py  # issue body rendering
└── scripts/
    └── seed.py                # seed one Customer/Project/Issue for local dev
```

---

## 4. Data model (authoritative)

`app/models/spec.py` — the Pydantic contract. The ORM row stores this as JSON plus a few
queryable columns (issue_id, project_id, status, approved_at).

```python
from __future__ import annotations
from enum import Enum
from datetime import datetime
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
    id: str                      # AC-1, AC-2 ...
    given: str
    when: str
    then: str
    status: CriterionStatus = CriterionStatus.DRAFT

class TestPlanItem(BaseModel):
    criterion_id: str            # references an existing AcceptanceCriterion.id
    assertion: str
    test_type: TestType

class OpenQuestion(BaseModel):
    id: str                      # Q-1, Q-2 ...
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
            ("Criteria \u2265 1", len(self.acceptance_criteria) >= 1),
            ("All criteria approved",
             len(self.acceptance_criteria) >= 1
             and all(c.status == CriterionStatus.APPROVED for c in self.acceptance_criteria)),
            ("Test plan covers criteria",
             len(self.acceptance_criteria) >= 1
             and all(c.id in covered for c in self.acceptance_criteria)),
            ("No open questions", all(q.resolved for q in self.open_questions)),
            ("Approved", self.approved_by is not None),
        ]

    def is_syncable(self) -> bool:
        # Derived from gate_items() so the rule lives in exactly one place.
        return all(passing for _, passing in self.gate_items())
```

`gate_items()` is the single source of truth for the gate; `is_syncable()` is derived from it,
and tests assert the truth table directly. Note the "Test plan covers criteria" check asserts
real *coverage* \u2014 every `AcceptanceCriterion.id` is referenced by at least one `TestPlanItem` \u2014
not merely that the test plan has as many items as there are criteria.

**Referential integrity is NOT a gate item.** A `TestPlanItem.criterion_id` pointing at a
non-existent criterion is a data bug, not a workflow milestone, so it stays out of the six gate
conditions (the coverage check above already catches the case that matters \u2014 a real criterion
with no test). Keep references valid at the mutation seams instead:
- `draft_test_plan()` iterates the criteria, so it emits only valid `criterion_id`s by construction.
- Deleting an `AcceptanceCriterion` cascade-drops its `TestPlanItem`s (and any criterion-scoped
  `OpenQuestion`s) \u2014 one helper on the spec; transient invalid states never need to be persisted.
- At the **AI-output parse boundary only** (`draft_spec` / `draft_test_plan`), a dangling
  `criterion_id` is treated as malformed output \u2192 the same typed error as bad JSON (or repaired
  there). Fail-fast belongs where untrusted model output enters, not on every construction.

---

## 5. The AI drafting contract

`app/services/refiner.py`. Three functions, all returning structured JSON parsed into the models:

- `draft_spec(raw_issue, project_context) -> RefinedSpec` — first pass. MUST emit an
  `OpenQuestion` for every domain assumption it cannot verify, rather than guessing.
- `draft_test_plan(spec) -> list[TestPlanItem]` — one item per acceptance criterion, choosing a
  `TestType`. Parity/property/behavioral preferred over manual where possible.
- `clarify(spec, question_id, answer) -> RefinedSpec` — folds a human answer into the spec,
  marks the question resolved, and may refine affected criteria.

Drafting prompts live as module constants. Rules baked into the system prompt:
- Output **only** valid JSON matching the target schema. No prose, no markdown fences.
- Never invent domain facts (asset names, formulas, business rules). Emit an `OpenQuestion`.
- Acceptance criteria use Given/When/Then.
- Be conservative on scope; prefer `out_of_scope` entries over silent assumptions.

All Anthropic calls are wrapped so tests can inject a fake client (dependency-injected, not patched).

---

## 6. Multi-agent / multi-worktree execution model

We use the parallel-worktree pattern. **Each work item is one PR, one branch, one Claude Code
session (Opus 4.8), in its own git worktree folder.** Independent items run in parallel worktrees;
sequential items (that touch the same files) chain inside one worktree off fresh `main` after merge.

```
Step 0: WI-0 (scaffold + ADR) merges FIRST — it writes the rules every later session reads.
        ┌────────────┬────────────┬───────────┬───────────┬───────────┐
   Worktree A    Worktree B    Worktree C  Worktree D  Worktree E
   (sequential)  (sequential)  (parallel)  (parallel)  (parallel)
   WI-1 → WI-2   WI-4 → WI-5   WI-6        WI-7        WI-3
        └────────────┴───────┬────┴───────────┴───────────┘
                    One review gate · Troy (human-merge every PR to main)
                                  │
                    WI-8 (sync) — approval-gated (touches GitHub push)
                    depends on WI-1, WI-2, WI-3, WI-5, WI-6 merged
                                  │
                             main → test deploy
```

(WI-3 depends only on the WI-1 model, so it runs in parallel in its own worktree once WI-1
has merged — or off WI-1's branch if you prefer not to wait.)

**Worktree setup (run once per worktree):**
```bash
git worktree add ../signal-wt-a -b wi-1-spec-model
git worktree add ../signal-wt-b -b wi-4-refiner-service
git worktree add ../signal-wt-c -b wi-6-console-template
git worktree add ../signal-wt-d -b wi-7-seed-and-config
git worktree add ../signal-wt-e -b wi-3-github-render
```

**Within each session, the agent roles (the "multi-agent" loop):**
1. **Implementer** — writes the code for the work item against this CLAUDE.md.
2. **Verifier** — a *separate* Claude Code invocation (fresh context) that is given only the WI
   acceptance criteria + the diff, and checks the implementation against the criteria. Does not
   share the implementer's context.
3. **Classifier** — reviews the verifier's findings, labels each real / false-positive /
   needs-human, and writes the PR description summarizing what passed.

Keep implementer and verifier in **separate sessions/contexts**. A self-review in the same context
is not an independent check.

**Collision rules:**
- WI-1 → WI-2 share `app/models/` and `app/routes/refining.py` → **sequential**, Worktree A.
- WI-4 → WI-5 share `app/services/refiner.py` → **sequential**, Worktree B.
- WI-6 (templates), WI-7 (seed/config), and WI-3 (github render) collide with nothing →
  **parallel**, C, D, and E.
- WI-8 depends on WI-1, WI-2, WI-3, WI-5, WI-6 all merged (WI-5 implies WI-4) → starts after the
  gate clears them.

---

## 7. Work items (the build, broken into PRs)

Each WI is sized for one focused session. Acceptance criteria are the PR's gate.

### WI-0 — Scaffold + ADR (merges first, blocks everything)
- Create repo layout (section 3), `pyproject.toml` (ruff + mypy + pytest), `.env.example`,
  `app/config.py` (pydantic-settings), `app/db.py` (async engine, `PRAGMA foreign_keys=ON`).
- Alembic initialized against SQLite. Note: the app runs async (`aiosqlite`), but Alembic
  migrations run synchronously — point `alembic.ini`/`env.py` at a sync driver
  (`sqlite:///signal.db`), or use an async `env.py` with `asyncio.run(...)`. Pick one and
  document it in the ADR so WI-2 follows the same pattern.
- `app/main.py` boots an empty FastAPI app with a `/healthz` route.
- **AC:** `uvicorn app.main:app` starts; `GET /healthz` → 200; `ruff check` and `mypy app` clean;
  `alembic upgrade head` runs on a fresh `signal.db`.

### WI-1 — Spec model + gate logic  *(Worktree A, first)*
- Implement `app/models/spec.py` exactly as section 4.
- **AC:** `test_spec_model.py` covers the `is_syncable()` truth table — every gate condition
  independently flipped; `gate_items()` returns 6 ordered pairs; a fully-populated approved spec
  returns `True`, and removing any single requirement returns `False`. Include the coverage
  regression: criteria `[AC-1, AC-2]` with a test plan referencing only `AC-1` → gate fails
  (the count-only check would have wrongly passed this).

### WI-2 — ORM + persistence + Alembic migration  *(Worktree A, after WI-1)*
- `app/models/orm.py`: `Customer`, `Project`, `Issue`, `RefinedSpecRow` (spec stored as JSON text
  column + queryable `issue_id`, `project_id`, `status`, `approved_at`).
- Repository functions: `get_spec(issue_id)`, `save_spec(spec)`, `list_refining_issues()`.
- Alembic migration creates the tables.
- **AC:** round-trip test — save a `RefinedSpec`, reload, assert equality; FK enforcement on;
  migration up/down clean.

### WI-3 — GitHub issue rendering  *(parallel-safe; depends on WI-1 model only)*
- `app/services/github_sync.py::render_issue_body(spec, signal_url) -> str` — Markdown body with
  Context / Expected / Actual / Reproduction / Acceptance Criteria / Test Plan + Signal footer.
- R1: actual push is behind `settings.github_sync_enabled` (default False) — render + log only.
- **AC:** `test_github_render.py` asserts every section present, ACs and test-plan items rendered,
  footer links back to the Signal issue URL. No network in tests.

### WI-4 — Refiner service: draft_spec  *(Worktree B, first)*
- `draft_spec()` with injected Anthropic client; system prompt per section 5; JSON parsed to
  `RefinedSpec`; every unverifiable assumption becomes an `OpenQuestion`.
- **AC:** `test_refiner.py` with a fake client returning canned JSON → produces a valid
  `RefinedSpec`; malformed JSON raises a typed error; at least one `OpenQuestion` survives parsing.
  A test-plan item with a dangling `criterion_id` in the model output is treated as malformed →
  same typed error (or repaired at the parse boundary).

### WI-5 — Refiner service: draft_test_plan + clarify  *(Worktree B, after WI-4)*
- `draft_test_plan(spec)` → one `TestPlanItem` per criterion with a sensible `TestType` (iterate
  the criteria so every `criterion_id` is valid by construction).
- `clarify(spec, question_id, answer)` → folds answer in, marks question resolved.
- Deleting an `AcceptanceCriterion` cascade-drops its `TestPlanItem`s (and criterion-scoped
  `OpenQuestion`s) via a single spec helper, so references never dangle.
- **AC:** every criterion id is referenced by at least one produced `TestPlanItem` (full coverage,
  not just count ≥); deleting a criterion removes its test items (no dangling refs); `clarify`
  flips `resolved=True` and stores the answer; resolving the last question makes `all(q.resolved)`
  true.

### WI-6 — Console template + HTMX wiring  *(Worktree C, parallel)*
- `refining_console.html` three-panel layout; partials for `gate_panel`, `spec_form`,
  `needs_input`. HTMX: editing a field PATCHes and swaps the gate panel; the sync button is
  rendered disabled unless `is_syncable()`.
- Follow the three-panel layout described in section 1 as the visual spec (left: raw feedback;
  middle: editable draft + `NEEDS_INPUT` flags; right: live gate panel). If a mockup file has been
  committed, link it here; otherwise section 1 is authoritative. Tabler outline icons; sentence case.
- **AC:** route test renders the console for a seeded issue; PATCHing `summary` returns the updated
  gate partial with "Summary set" passing; sync button has `disabled` until gate clears.

### WI-7 — Seed + config + dev runbook  *(Worktree D, parallel)*
- `scripts/seed.py` inserts one Customer (H&H Oil), one Project (ops-hub), one Issue (#142 fuel
  total bug) so the console has data on first run.
- README section: env setup (micromamba `signal`), `.env`, run, seed, test.
- **AC:** fresh clone → `micromamba create` → `alembic upgrade head` → `python scripts/seed.py` →
  console shows issue #142.

### WI-8 — Wire the routes end-to-end + sync action  *(after WI-1,2,3,5,6 merged; approval-gated)*
- `routes/refining.py`: GET console, PATCH field (re-eval gate), POST `draft` (calls refiner),
  POST `draft-test-plan`, POST `resolve-question`, POST `approve`, POST `sync` (guarded by
  `is_syncable()` server-side; renders issue body; pushes only if `github_sync_enabled`).
- **AC:** full flow test — seed → draft → answer question → approve criteria → draft test plan →
  approve → gate green → sync produces a rendered issue body. Server rejects `sync` if
  `is_syncable()` is False even if the button is forced.
- **Approval gate:** this PR touches the outbound GitHub path → Troy signs off explicitly.

---

## 8. Definition of done (every PR)

- `ruff check` + `ruff format --check` clean.
- `mypy app` clean.
- `pytest` green; new code has tests; no network calls in tests (Anthropic + GitHub mocked).
- The work item's acceptance criteria each map to a passing test.
- PR description (written by the classifier role) lists: what changed, which ACs are covered by
  which tests, and any verifier findings marked needs-human.
- Human-merged to `main` by Troy. No agent self-merges.

---

## 9. Commands

```bash
# env
micromamba create -n signal python=3.12 -y && micromamba activate signal
pip install fastapi uvicorn jinja2 sqlalchemy aiosqlite alembic pydantic pydantic-settings anthropic
pip install --group dev pytest pytest-asyncio httpx ruff mypy   # or plain pip install

# db
alembic upgrade head
python scripts/seed.py

# run
uvicorn app.main:app --reload --port 8060

# quality (run before every PR)
ruff check . && ruff format --check . && mypy app && pytest -q
```

---

## 10. Out of scope for Release 1

- Postgres / Azure SQL (R2 — swap the SQLAlchemy URL + re-run migrations).
- Live bidirectional GitHub webhook sync back into Signal (R2).
- Multi-user auth / Entra SSO (schema is multi-tenant-aware; enforcement is R2).
- The VPH harness itself — R1 only emits the test plan; wiring assertions to a live harness is R2.
- Visual-spec mockup authoring surface (R2).

Keep R1 tight: get raw feedback → refined, gated spec → rendered GitHub issue working end-to-end,
on SQLite, single user.
