from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.orm import get_spec, save_spec
from app.models.spec import RefinedSpec

# Templates live alongside the app package (app/templates).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# WI-6 ships the minimal console + summary edit. WI-8 extends this same router
# with draft / draft-test-plan / resolve-question / approve / sync actions.
router = APIRouter(prefix="/refining", tags=["refining"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load_spec(session: AsyncSession, issue_id: int) -> RefinedSpec:
    spec = await get_spec(session, issue_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No refined spec for issue {issue_id}")
    return spec


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
    body = (await request.body()).decode("utf-8")
    fields = parse_qs(body, keep_blank_values=True)
    values = fields.get("summary", [])
    summary = values[0] if values else ""
    spec = await _load_spec(session, issue_id)
    spec.summary = summary
    await save_spec(session, spec)
    return templates.TemplateResponse(
        request,
        "partials/gate_panel.html",
        {"spec": spec},
    )
