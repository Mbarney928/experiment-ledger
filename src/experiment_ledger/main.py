"""Application factory: wires settings, database, middleware, error handling and routes."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from experiment_ledger import __version__
from experiment_ledger.api import routes
from experiment_ledger.config import Settings, get_settings
from experiment_ledger.db import Base, make_engine, make_session_factory
from experiment_ledger.log import configure_logging, get_logger
from experiment_ledger.security import SlidingWindowRateLimiter
from experiment_ledger.services.errors import Conflict, DomainError, InvalidState, NotFound
from experiment_ledger.services.stats import StatsError

log = get_logger("experiment_ledger")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}
# The API serves JSON only; Swagger UI needs its CDN assets, so docs get a looser policy.
API_CSP = "default-src 'none'; frame-ancestors 'none'"

_STATUS = {NotFound: 404, Conflict: 409, InvalidState: 409}


def create_app(settings: Settings | None = None, *, create_tables: bool = False) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="experiment-ledger",
        version=__version__,
        description=(
            "Register A/B tests, record cumulative results and get peeking-safe decisions "
            "using group-sequential (O'Brien-Fleming alpha-spending) boundaries."
        ),
    )
    engine = make_engine(settings.database_url)
    if create_tables:
        Base.metadata.create_all(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    attempts, window = settings.login_rate
    app.state.login_limiter = SlidingWindowRateLimiter(attempts, window)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("Content-Security-Policy", API_CSP)
        log.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=_STATUS.get(type(exc), 400))

    @app.exception_handler(StatsError)
    async def stats_error(_request: Request, exc: StatsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo field locations and messages, never the submitted values (could be passwords).
        errors = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    for router in (routes.health, routes.auth, routes.users, routes.planning, routes.experiments):
        app.include_router(router)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory experiment_ledger.main:app_factory`."""
    return create_app()
