"""The bounded failover loop: the only place a provider call is retried.

``execute_with_failover`` runs select -> attempt -> classify -> mark ->
rebuild -> re-select, bounded by ``retry_budget``. LiteLLM is used as a
single-attempt transport only.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings
from app.core.redact import redact
from app.pool.models import Provider
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.provider.adapter import AttemptResult, CandidateGoneError, ProviderAttemptError
from app.provider.adapter import attempt as adapter_attempt
from app.router.classify import Classification, classify
from app.router.exhaustion import PoolExhaustedError
from app.router.rebuild import RebuildHook, identity_rebuild
from app.router.retry_hint import retry_after_seconds
from app.router.selection import select

logger = logging.getLogger("palimpsest.failover")

SYSTEM_OPERATOR_ID = "gateway:auth_rejected"
"""``last_modified_by`` on a key the gateway suspended after a 401/403."""


async def _chain(first: object, rest: object):
    """The primed first chunk, then the rest of the provider's stream."""
    yield first
    async for chunk in rest:
        yield chunk


async def _empty():
    return
    yield  # makes this an async generator


async def _close_quietly(iterator: object) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        pass


async def _prime_stream(result: AttemptResult, provider: Provider) -> AttemptResult:
    """Read a stream's first chunk before anything is sent to the client.

    Providers often accept a stream and then fail on the first read (rate
    limit, overload). Caught here it is an ordinary failed attempt that can
    fail over; later it would break a reply that nothing can retry.
    """
    iterator = result.response.__aiter__()
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration:
        return AttemptResult(ok=True, response=_empty(), headers=result.headers)
    except Exception as exc:
        await _close_quietly(iterator)
        status = getattr(exc, "status_code", None)
        error = ProviderAttemptError(
            status if isinstance(status, int) else None,
            "stream_start_error",
            str(getattr(exc, "message", exc))[:500],
            provider,
        )
        return AttemptResult(ok=False, error=error, headers=result.headers)
    return AttemptResult(ok=True, response=_chain(first, iterator), headers=result.headers)


@dataclass(frozen=True)
class AttemptRecord:
    """One attempt's outcome. Key and provider are for operator logs only."""

    key_id: str
    provider: Provider
    classification: str | None  # None on success
    outcome: str  # "success" | "failed" | "skipped"
    status_code: int | None
    timestamp: float | None = None


@dataclass(frozen=True)
class FailoverTrace:
    """Every attempt of one request, for settlement and the request log."""

    model: str
    attempts: tuple[AttemptRecord, ...]
    terminal: str  # "success" | "fatal" | "stream_interrupted" | "budget_exhausted" | "pool_exhausted"
    retry_count: int
    classification: Classification | None = None
    start_time: float | None = None
    end_time: float | None = None


async def _earliest_next_reset(registry: PoolRegistry, model: str) -> datetime | None:
    """When a key serving ``model`` is next expected back, for the retry hint.

    Prefers the end of a running cooldown or exhaustion; falls back to the
    earliest quota-window reset among all keys serving the model.
    """
    available = await registry.next_available_at(model)
    if available is not None:
        return available

    all_keys = await registry.list()
    now = datetime.now(UTC)
    resets = [record.next_reset_at(now) for record in all_keys if model in record.models]
    return min(resets) if resets else None


async def _exhausted(
    registry: PoolRegistry,
    model: str,
    code: str,
    attempts: list[AttemptRecord],
    retry_count: int,
    start_time: float,
    classification: Classification | None = None,
) -> PoolExhaustedError:
    next_reset = await _earliest_next_reset(registry, model)
    error = PoolExhaustedError(code, next_reset, retry_count=retry_count)
    error.trace = FailoverTrace(
        model=model,
        attempts=tuple(attempts),
        terminal=code,
        retry_count=retry_count,
        classification=classification,
        start_time=start_time,
        end_time=time.monotonic(),
    )
    return error


async def execute_with_failover(
    registry: PoolRegistry,
    model: str,
    messages: list[dict],
    *,
    session: object | None = None,
    rebuild_hook: RebuildHook | None = None,
    first_byte_committed: bool = False,
    **params: object,
) -> tuple[AttemptResult, FailoverTrace]:
    """Run the bounded failover loop for one logical request.

    Returns ``(result, trace)`` on success or on a terminal failure (``fatal``,
    or any failure once ``first_byte_committed``); the caller renders
    ``result.error`` when ``not result.ok``. Raises ``PoolExhaustedError``,
    with ``.trace`` attached, when no key is left or the budget is spent.
    """
    hook = rebuild_hook or identity_rebuild
    settings = get_settings()
    retries_left = settings.retry_budget
    max_excluded = settings.retry_budget * 2
    excluded: set[str] = set()
    attempts: list[AttemptRecord] = []
    retry_count = 0
    preferred_provider: Provider | None = None
    start_time = time.monotonic()

    while True:
        candidate = None
        if len(excluded) <= max_excluded:
            candidate = await select(
                registry, model, excluded=frozenset(excluded), preferred_provider=preferred_provider
            )
        if candidate is None:
            raise await _exhausted(
                registry, model, "pool_exhausted", attempts, retry_count, start_time
            )

        try:
            result = await adapter_attempt(registry, candidate.key_id, model, messages, **params)
        except CandidateGoneError:
            excluded.add(candidate.key_id)
            attempts.append(
                AttemptRecord(
                    key_id=candidate.key_id,
                    provider=candidate.provider,
                    classification=None,
                    outcome="skipped",
                    status_code=None,
                    timestamp=time.monotonic(),
                )
            )
            continue

        if result.ok and params.get("stream"):
            result = await _prime_stream(result, candidate.provider)

        if result.ok:
            attempts.append(
                AttemptRecord(
                    key_id=candidate.key_id,
                    provider=candidate.provider,
                    classification=None,
                    outcome="success",
                    status_code=None,
                    timestamp=time.monotonic(),
                )
            )
            trace = FailoverTrace(
                model=model, attempts=tuple(attempts), terminal="success",
                retry_count=retry_count, start_time=start_time,
                end_time=time.monotonic(),
            )
            return result, trace

        classification = classify(result.error)
        # The provider's own text says which limit was hit. Operator log only,
        # key-shaped strings removed, on one line.
        logger.warning(
            "provider attempt failed key=%s provider=%s model=%s status=%s class=%s detail=%s",
            candidate.key_id,
            candidate.provider.value,
            model,
            result.error.status_code,
            classification.state,
            " ".join(redact(result.error.message or "").split())[:600],
        )
        try:
            if classification.state == "transient":
                # Only this model cools down; the key's other models stay
                # routable. The provider's own retry hint wins over the default.
                await registry.mark_cooldown(
                    candidate.key_id,
                    model=model,
                    seconds=retry_after_seconds(result.error.message, result.headers),
                )
            elif classification.state == "exhausted":
                await registry.mark_exhausted(candidate.key_id)
            elif classification.state == "rejected":
                # The credential itself was refused: suspend it for the operator.
                await registry.suspend(candidate.key_id, operator_id=SYSTEM_OPERATOR_ID)
        except KeyNotFoundError:
            pass

        attempts.append(
            AttemptRecord(
                key_id=candidate.key_id,
                provider=candidate.provider,
                classification=classification.state,
                outcome="failed",
                status_code=result.error.status_code,
                timestamp=time.monotonic(),
            )
        )
        excluded.add(candidate.key_id)
        preferred_provider = candidate.provider

        if classification.state == "fatal" or first_byte_committed:
            terminal = (
                "stream_interrupted" if first_byte_committed and classification.state != "fatal"
                else "fatal"
            )
            trace = FailoverTrace(
                model=model, attempts=tuple(attempts), terminal=terminal,
                retry_count=retry_count, classification=classification,
                start_time=start_time, end_time=time.monotonic(),
            )
            return result, trace

        if retries_left == 0:
            raise await _exhausted(
                registry, model, "budget_exhausted", attempts, retry_count,
                start_time, classification,
            )

        retries_left -= 1
        retry_count += 1
        messages = await hook(messages, session, retry_count)
