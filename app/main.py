from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings


def create_app() -> FastAPI:
    """Application factory. Routers are registered here as work items land."""
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
