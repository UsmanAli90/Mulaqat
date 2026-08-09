"""Application factory.

There is no global `app` built at import time. `create_app()` constructs a
fresh instance on demand, which is what lets tests build an app with
overridden dependencies without mutating shared state between test runs.

Run locally with:  uv run uvicorn app.main:app --reload
Uvicorn accepts the factory result via the module-level `app` at the bottom.
"""

from fastapi import FastAPI

from app.api import health
from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure a FastAPI application.

    Args:
        settings: Injected configuration. Defaults to the cached process
            settings; tests pass an explicit object to run against the test
            database or a different environment.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="Booking",
        version="0.1.0",
        # Hide the interactive docs in production. They are useful locally and
        # an unnecessary disclosure of internal routes on a public host.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Routers are the rough equivalent of `draw` blocks in routes.rb: each
    # module owns its own paths and gets mounted here.
    app.include_router(health.router)

    return app


app = create_app()
