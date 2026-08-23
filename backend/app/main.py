"""FastAPI application factory.

Configuration is validated before the application is able to serve anything. A missing or
malformed required setting stops the process here with a clear message rather than surfacing as a
request-time failure later.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import agent, merchant, webhooks
from app.correlation import HEADER, new_correlation_id, set_correlation_id
from app.db.base import session_scope
from app.errors import AgentError, agent_error_handler, unhandled_error_handler
from app.logging import configure_logging, get_logger
from app.settings import ConfigurationError, settings

logger = get_logger(__name__)


def validate_startup() -> None:
    """Check everything the application will need before it accepts a single request."""
    problems: list[str] = []

    for label, relative in (
        ("TRUST_REGISTRY_PATH", settings.TRUST_REGISTRY_PATH),
        ("CATALOG_SEED_PATH", settings.CATALOG_SEED_PATH),
        ("POLICY_TERMS_PATH", settings.POLICY_TERMS_PATH),
    ):
        if not settings.resolve(relative).exists():
            problems.append(f"{label} points at {settings.resolve(relative)}, which does not exist")

    try:
        from app.trust.registry import get_registry

        registry = get_registry()
        if not registry.authorities:
            problems.append("the trust registry declares no issuing authorities")
    except Exception as exc:  # any registry problem stops startup
        problems.append(f"the trust registry is unusable: {exc}")

    try:
        from app.keys import key_directory, merchant_key

        key_directory().mkdir(parents=True, exist_ok=True)
        merchant_key()
    except Exception as exc:  # without a signing key nothing can be signed
        problems.append(f"the merchant signing key could not be loaded or generated: {exc}")

    if problems:
        raise ConfigurationError(
            "Dwarpal cannot start:\n" + "\n".join(f"  {p}" for p in problems)
        )


def bootstrap_database() -> None:
    from app.catalog.policy_terms import ensure_active_terms
    from app.db.bootstrap import create_schema, seed_catalog

    create_schema()
    with session_scope() as session:
        seeded = seed_catalog(session)
        terms = ensure_active_terms(session)
    logger.info(
        "database ready",
        extra={"context": {"products_seeded": seeded, "policy_hash": terms.content_hash}},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    del app
    configure_logging()
    validate_startup()
    bootstrap_database()
    logger.info(
        "dwarpal started",
        extra={"context": {"env": settings.APP_ENV, "merchant": settings.MERCHANT_ID}},
    )
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Dwarpal",
        description="The AP2 merchant endpoint for Razorpay.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(HEADER)
        identifier = incoming or new_correlation_id()
        set_correlation_id(identifier)
        response = await call_next(request)
        response.headers[HEADER] = identifier
        return response

    # The dashboard reaches the backend through its own same-origin proxy, so no browser origin
    # needs to be allowed here. Server-to-server agents are unaffected by CORS.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_exception_handler(AgentError, agent_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    application.include_router(agent.router)
    application.include_router(webhooks.router)
    application.include_router(merchant.router)

    @application.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "merchant": settings.MERCHANT_ID}

    return application


try:
    app = create_app()
except ConfigurationError as exc:  # pragma: no cover - exercised by running the process
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from exc
