from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from geo_api.api.errors import install_error_handlers
from geo_api.api.routes import router
from geo_api.config import Settings, get_settings
from geo_api.db.session import close_database
from geo_api.middleware.admission import RequestBoundaryMiddleware
from geo_api.processing.storage import cleanup_abandoned_workspaces, prepare_temp_root


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        root = prepare_temp_root(settings)
        cleanup_abandoned_workspaces(root)
        app.state.temp_root = root
        app.state.settings = settings
        try:
            yield
        finally:
            await close_database()

    app = FastAPI(title="Geo API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestBoundaryMiddleware, settings=settings)
    app.include_router(router)
    install_error_handlers(app)
    return app


app = create_app()
