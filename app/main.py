"""Application factory, startup/shutdown and error envelopes."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.alerts import alert_loop
from app.api.accounts import router as accounts_router
from app.api.chat import router as chat_router
from app.api.dashboard import mount_dashboard
from app.api.keys import router as keys_router
from app.api.models import router as models_router
from app.api.operator import router as operator_router
from app.api.operator_accounts import router as operator_accounts_router
from app.api.operator_config import router as config_router
from app.api.operator_insights import router as operator_insights_router
from app.api.operator_usage import router as operator_usage_router
from app.api.pool_routes import ops_router as pool_ops_router
from app.api.pool_routes import router as pool_router
from app.api.sessions import router as sessions_router
from app.config import apply_persisted_overrides, get_settings
from app.core.ids import new_id
from app.errors import GatewayError
from app.pool.models import KeyCreate
from app.pool.registry import DuplicateKeyError, PoolRegistry
from app.store.config import ConfigStore
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore
from app.usage.store import drain as drain_usage

_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _request_id_header(request: Request) -> dict[str, str]:
    """Echo a well-formed client X-Request-ID, otherwise generate one."""
    incoming = request.headers.get("X-Request-ID")
    if not incoming or not _CLIENT_REQUEST_ID.fullmatch(incoming):
        incoming = new_id()
    return {"X-Request-ID": incoming}


async def init_store(app: FastAPI) -> None:
    """Connect to Redis, re-apply config overrides and seed bootstrap keys."""
    settings = get_settings()
    redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    app.state.store = RedisStore(redis_client)

    # Operator overrides survive a restart.
    overrides = await ConfigStore(redis_client).get_overrides()
    if overrides:
        apply_persisted_overrides(overrides)

    # Only the key's ciphertext and hash are persisted, never the raw value.
    bootstrap_key = os.environ.get("PALIMPSEST_GATE_OPERATOR_KEY")
    if bootstrap_key:
        await OperatorStore(redis_client).seed_bootstrap_key(bootstrap_key)

    await seed_provider_key(redis_client)


async def seed_provider_key(redis_client) -> None:
    """Add ``PALIMPSEST_PROVIDER_API_KEY`` to the pool, idempotently.

    A key already in the pool is left alone, so restarts never duplicate it
    and an operator's suspend survives. Failures are logged, never fatal.
    """
    settings = get_settings()
    if not settings.provider_api_key.strip():
        return

    log = logging.getLogger("palimpsest.app")
    try:
        data = KeyCreate(
            provider=settings.provider_name,
            models=[settings.provider_model],
            key=settings.provider_api_key,
        )
    except ValidationError:
        log.error(
            "PALIMPSEST_PROVIDER_API_KEY not added: invalid PALIMPSEST_PROVIDER_NAME "
            "or PALIMPSEST_PROVIDER_MODEL (providers: openai, anthropic, google, openrouter, mistral, groq, custom)"
        )
        return
    try:
        await PoolRegistry(redis_client).create(data, operator_id="env:PALIMPSEST_PROVIDER_API_KEY")
        log.info("added PALIMPSEST_PROVIDER_API_KEY to the pool for model %s", settings.provider_model)
    except DuplicateKeyError:
        pass
    except Exception:
        log.error("failed to add PALIMPSEST_PROVIDER_API_KEY to the pool", exc_info=True)


async def refresh_config_overrides(redis_client, interval: float) -> None:
    """Re-read operator overrides every ``interval`` seconds, forever.

    A config PATCH applies on the instance that served it; this loop carries
    it to every other instance.
    """
    store = ConfigStore(redis_client)
    while True:
        await asyncio.sleep(interval)
        try:
            overrides = await store.get_overrides()
            if overrides:
                apply_persisted_overrides(overrides)
        except Exception:
            logging.getLogger("palimpsest.config").warning(
                "config override refresh failed", exc_info=True
            )


async def close_store(app: FastAPI) -> None:
    """Close the Redis connection on shutdown."""
    if hasattr(app.state, "store"):
        await app.state.store.redis.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Store startup/shutdown and the config refresh loop."""
    await init_store(app)
    refresher = None
    interval = get_settings().config_refresh_seconds
    if interval > 0:
        refresher = asyncio.create_task(
            refresh_config_overrides(app.state.store.redis, interval)
        )
    alerter = asyncio.create_task(alert_loop(app.state.store.redis))
    yield
    alerter.cancel()
    try:
        await alerter
    except asyncio.CancelledError:
        pass
    if refresher is not None:
        refresher.cancel()
        try:
            await refresher
        except asyncio.CancelledError:
            pass
    # Let in-flight usage records land before the connection closes.
    try:
        await asyncio.wait_for(drain_usage(), timeout=5)
    except TimeoutError:
        logging.getLogger("palimpsest.app").warning("usage records still pending at shutdown")
    await close_store(app)


def _validation_param(exc: RequestValidationError) -> str | None:
    """Best-effort name of the offending field, for the envelope's ``param``."""
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", ())]
        named = [part for part in loc if part != "body"]
        if named:
            return named[-1]
    return None


def _envelope(error_type: str, message: str, code: str, param: str | None = None) -> dict:
    return {"error": {"type": error_type, "message": message, "param": param, "code": code}}


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Palimpsest Gateway",
        description="OpenAI-compatible LLM gateway with non-disclosure policy",
        version=settings.project_version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.created_at = int(datetime.now(UTC).timestamp())

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Answer browsers' automatic favicon request with an empty 204."""
        return Response(status_code=204)

    @app.get("/healthz")
    async def health_check() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, str]:
        """Service info."""
        return {
            "service": "palimpsest-gateway",
            "version": settings.project_version,
            "description": "OpenAI-compatible LLM gateway",
            "endpoints": (
                "/v1/accounts,/v1/accounts/{id}/keys,/v1/chat/completions,/v1/models,/v1/sessions,"
                "/operator/v1/keys,/v1/operator/pool/keys,"
                "/v1/operator/pool/utilization,/v1/operator/pool/failover-events,"
                "/healthz,/dashboard/"
            ),
        }

    app.include_router(accounts_router)
    app.include_router(keys_router)
    app.include_router(chat_router)
    app.include_router(models_router)
    app.include_router(sessions_router)
    app.include_router(operator_router)
    app.include_router(operator_accounts_router)
    app.include_router(config_router)
    app.include_router(operator_usage_router)
    app.include_router(operator_insights_router)
    app.include_router(pool_router)
    app.include_router(pool_ops_router)

    mount_dashboard(app)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        """Render a ``GatewayError`` in the OpenAI error envelope."""
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.type, exc.message, exc.code, exc.param),
            # The route's own X-Request-ID wins over a generated one.
            headers={**_request_id_header(request), **(exc.headers or {})},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Malformed request bodies: 422 in the OpenAI envelope."""
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "invalid_request_error", "Invalid request.", "invalid_request",
                _validation_param(exc),
            ),
            headers=_request_id_header(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Framework HTTP errors (404/405/...) in the OpenAI envelope."""
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        client_error = exc.status_code < 500
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                "invalid_request_error" if client_error else "api_error",
                str(exc.detail) if client_error else "Internal error.",
                code,
            ),
            headers={**(exc.headers or {}), **_request_id_header(request)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: a non-disclosing 500."""
        logging.getLogger("palimpsest.app").error(
            "unhandled exception on %s %s", request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(
            status_code=500,
            content=_envelope("api_error", "Internal error.", "internal_error"),
            headers=_request_id_header(request),
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
