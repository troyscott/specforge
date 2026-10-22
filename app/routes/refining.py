from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models.orm import get_spec, save_spec
from app.models.spec import CriterionStatus, RefinedSpec
from app.services import github_sync, refiner

# Templates live alongside the app package (app/templates).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# WI-6 ships the minimal console + summary edit. WI-8 extends this same router
# with draft / draft-test-plan / resolve-question / approve / sync actions.
router = APIRouter(prefix="/refining", tags=["refining"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --- Anthropic client dependency (overridable in tests) ----------------------
#
# The refiner takes an injected client satisfying refiner.RefinerClient. In
# production we build the real Anthropic SDK client lazily (and cache it) so the
# import + key resolution only happens when a draft is actually requested. Tests
# override this dependency with a fake client — so there is NO network in tests,
# and no client is constructed unless `POST draft` is actually called.


@lru_cache
def _build_anthropic_client() -> refiner.RefinerClient:
    """Construct the real Anthropic client from settings. Never called in tests."""
    import anthropic  # imported lazily so tests/CI need no key to import this module

    settings = get_settings()
    # Key comes from settings/env (pydantic-settings reads ANTHROPIC_API_KEY).
    # If unset, the SDK still constructs but calls would fail — which is correct:
    # drafting requires an explicitly configured key.
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    # The SDK's `messages` is a read-only property; RefinerClient is a structural
    # Protocol describing only the slice we call. The shapes are compatible at
    # runtime — cast to satisfy the (settable-attribute) Protocol for mypy.
    return cast(refiner.RefinerClient, client)


def get_refiner_client() -> refiner.RefinerClient:
    """FastAPI dependency yielding the Anthropic-compatible refiner client.

    Overridden in tests via ``app.dependency_overrides`` with a fake client so no
    network call is ever made.
    """
    return _build_anthropic_client()


RefinerClientDep = Annotated[refiner.RefinerClient, Depends(get_refiner_client)]


# --- helpers -----------------------------------------------------------------


async def _load_spec(session: AsyncSession, issue_id: int) -> RefinedSpec:
    spec = await get_spec(session, issue_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No refined spec for issue {issue_id}")
    return spec


async def _form(request: Request) -> dict[str, list[str]]:
    """Parse the urlencoded HTMX form body into a multidict-like dict.

    HTMX posts ``application/x-www-form-urlencoded`` by default, so R1 parses it
    directly and needs no multipart dependency.
    """
    body = (await request.body()).decode("utf-8")
    return parse_qs(body, keep_blank_values=True)


def _first(fields: dict[str, list[str]], key: str, default: str = "") -> str:
    values = fields.get(key, [])
    return values[0] if values else default


def _gate_panel(request: Request, spec: RefinedSpec) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/gate_panel.html", {"spec": spec})


def _spec_form(request: Request, spec: RefinedSpec) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/spec_form.html", {"spec": spec})


# --- routes ------------------------------------------------------------------


@router.get("/{issue_id}", response_class=HTMLResponse)
async def console(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Render the full three-panel refining console for an issue."""
    spec = await _load_spec(session, issue_id)
    return templates.TemplateResponse(
        request,
        "refining_console.html",
        {"spec": spec},
    )


@router.patch("/{issue_id}/summary", response_class=HTMLResponse)
async def patch_summary(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Update the spec summary and return the recomputed gate panel partial.

    Parses the urlencoded form body (HTMX default) directly, so R1 needs no
    extra multipart dependency. WI-8 may revisit if richer field handling lands.
    """
    fields = await _form(request)
    summary = _first(fields, "summary")
    spec = await _load_spec(session, issue_id)
    spec.summary = summary
    await save_spec(session, spec)
    return _gate_panel(request, spec)


@router.post("/{issue_id}/draft", response_class=HTMLResponse)
async def draft(
    request: Request,
    issue_id: int,
    session: SessionDep,
    client: RefinerClientDep,
) -> HTMLResponse:
    """First-pass AI draft for the issue. Calls the refiner with the injected client.

    Reuses the existing issue_id/project_id and overwrites the stored spec with the
    AI draft (preserving the queryable issue/project). Returns the spec form so the
    middle panel (fields + NEEDS_INPUT flags) re-renders with the drafted content.
    """
    spec = await _load_spec(session, issue_id)
    raw_issue = spec.context or ""
    project_context = f"Project: {spec.project_id}"

    drafted = refiner.draft_spec(raw_issue, project_context, client=client)
    # Pin the draft to this issue/project regardless of what the model echoed back,
    # and preserve the raw feedback (the left panel reads spec.context verbatim).
    drafted.issue_id = spec.issue_id
    drafted.project_id = spec.project_id
    if not drafted.context:
        drafted.context = spec.context

    await save_spec(session, drafted)
    return _spec_form(request, drafted)


@router.post("/{issue_id}/draft-test-plan", response_class=HTMLResponse)
async def draft_test_plan(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Draft one TestPlanItem per acceptance criterion (deterministic; no client)."""
    spec = await _load_spec(session, issue_id)
    spec.test_plan = refiner.draft_test_plan(spec)
    await save_spec(session, spec)
    return _gate_panel(request, spec)


@router.post("/{issue_id}/resolve-question", response_class=HTMLResponse)
async def resolve_question(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Fold a human answer into the spec, marking the open question resolved."""
    fields = await _form(request)
    question_id = _first(fields, "question_id")
    answer = _first(fields, "answer")
    if not question_id:
        raise HTTPException(status_code=422, detail="question_id is required")

    spec = await _load_spec(session, issue_id)
    try:
        spec = refiner.clarify(spec, question_id, answer)
    except refiner.RefinerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await save_spec(session, spec)
    return _gate_panel(request, spec)


@router.post("/{issue_id}/approve", response_class=HTMLResponse)
async def approve(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Approve the spec: mark every acceptance criterion APPROVED and set approved_by.

    Designed so the gate can reach green once the other conditions (summary,
    criteria present, test plan, no open questions) are satisfied.
    """
    fields = await _form(request)
    approved_by = _first(fields, "approved_by") or "troy"

    spec = await _load_spec(session, issue_id)
    for criterion in spec.acceptance_criteria:
        criterion.status = CriterionStatus.APPROVED
    spec.approved_by = approved_by
    spec.approved_at = datetime.now(UTC)
    await save_spec(session, spec)
    return _gate_panel(request, spec)


@router.post("/{issue_id}/sync", response_class=HTMLResponse)
async def sync(
    request: Request,
    issue_id: int,
    session: SessionDep,
) -> HTMLResponse:
    """Sync the refined spec to GitHub — server-side gated by ``is_syncable()``.

    The gate is enforced HERE on the server, not just in the disabled button: a
    forced request on a non-syncable spec is rejected with 409, and no issue body
    is rendered. When syncable, the GitHub issue body is rendered; the actual push
    stays behind ``settings.github_sync_enabled`` (default False — render + log
    only, no network) per R1 scope.
    """
    spec = await _load_spec(session, issue_id)

    # SERVER-SIDE gate: read is_syncable() from the model (single source of truth).
    if not spec.is_syncable():
        raise HTTPException(
            status_code=409,
            detail="Spec is not syncable: the gate has not cleared.",
        )

    signal_url = str(request.url_for("console", issue_id=spec.issue_id))
    # R1: render + log only; push happens only if github_sync_enabled (we do not
    # enable it). No outbound network call is made here.
    issue_body = github_sync.sync_issue(spec, signal_url)

    return templates.TemplateResponse(
        request,
        "partials/sync_result.html",
        {"spec": spec, "issue_body": issue_body},
    )
