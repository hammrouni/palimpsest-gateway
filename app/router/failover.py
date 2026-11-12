"""The bounded failover loop: the only place a provider call is retried.

``execute_with_failover`` runs select -> attempt -> classify -> mark ->
rebuild -> re-select, bounded by ``retry_budget``. LiteLLM is used as a
single-attempt transport only.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import get_settings
from app.core.redact import redact
from app.pool.models import Provider
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.provider.adapter import AttemptResult, CandidateGoneError, ProviderAttemptError
from app.provider.adapter import attempt as adapter_attempt
from app.router.classify import (
    Classification,
    classify,
    is_connectivity_error,
    model_quota_limit,
    pause_reason,
)
from app.router.exhaustion import PoolExhaustedError
from app.router.learning import (
    needs_from_params,
    note_attempt,
    record_failure,
    record_success,
)
from app.router.rebuild import RebuildHook, identity_rebuild
from app.router.retry_hint import get_quota_reset_seconds, retry_after_seconds
from app.router.selection import select
from app.usage.store import track

logger = logging.getLogger("palimpsest.failover")

# How much of a stream-start error is kept for classification (logs cut it shorter).
_STREAM_ERROR_CHARS = 8000

SYSTEM_OPERATOR_ID = "gateway:auth_rejected"
"""``last_modified_by`` on a key the gateway suspended after a 401/403."""


def _pause_text(state: str, seconds: int | None) -> str:
    """How long the key or model is held back, for the operator log."""
    if state == "fatal":
        return "none"
    if state == "rejected":
        return "until-operator-resumes"
    if seconds is None:
        return "default"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = ((hours, "h"), (minutes, "m"), (secs, "s"))
    return "".join(f"{n}{u}" for n, u in parts if n) or "0s"


async def _pause_model_for_provider(
    registry: PoolRegistry,
    candidate,
    model: str,
    seconds: int,
    reason: str,
) -> None:
    """Pause ``model`` on every other active key of the candidate's provider."""
    for record in await registry.list():
        if record.id == candidate.key_id or record.provider != candidate.provider:
            continue
        try:
            await registry.mark_cooldown(record.id, model=model, seconds=seconds, reason=reason)
        except KeyNotFoundError:
            continue


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
    """Read a stream up to its first content before anything is sent to the client.

    Providers often accept a stream and then fail on the first read (rate
    limit, overload). Caught here it is an ordinary failed attempt that can
    fail over; later it would break a reply that nothing can retry.

    Priming stops at the first chunk that carries text or tool calls. A
    role-only chunk does not count, so a failure after it still fails over.
    """
    iterator = result.response.__aiter__()
    primed_chunks: list = []
    try:
        async for chunk in iterator:
            primed_chunks.append(chunk)
            delta = getattr(chunk.choices[0], "delta", None) if chunk.choices else None
            if getattr(delta, "content", None) or getattr(delta, "tool_calls", None):
                break
    except Exception as exc:
        await _close_quietly(iterator)
        status = getattr(exc, "status_code", None)
        error = ProviderAttemptError(
            status if isinstance(status, int) else None,
            "stream_start_error",
            # Long enough for the provider's details: Google names the model a
            # quota belongs to only after ~450 characters of preamble.
            str(getattr(exc, "message", exc))[:_STREAM_ERROR_CHARS],
            provider,
        )
        return AttemptResult(ok=False, error=error, headers=result.headers)

    return AttemptResult(
        ok=True, response=_chain(primed_chunks, iterator), headers=result.headers
    )


async def _chain(first_chunks: list, rest: object):
    """The primed chunks, then the rest of the provider's stream."""
    for chunk in first_chunks:
        yield chunk
    async for chunk in rest:
        yield chunk


@dataclass(frozen=True)
class AttemptRecord:
    """One attempt's outcome. Key and provider are for operator logs only."""

    key_id: str
    provider: Provider
    classification: str | None  # None on success
    outcome: str  # "success" | "failed" | "skipped"
    status_code: int | None
    timestamp: float | None = None
    # The model asked of that key and how long the attempt took; for the call history.
    model: str | None = None
    ms: int | None = None


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


def _notify(observer: Callable[[dict], None] | None, event: dict) -> None:
    """Tell the live view about an attempt; whatever it does cannot touch the request."""
    if observer is None:
        return
    try:
        observer(event)
    except Exception:
        logger.debug("live observer failed", exc_info=True)


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
    serving = registry.serving_models(all_keys)
    resets = [record.next_reset_at(now) for record in all_keys if model in serving[record.id]]
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


async def _short_wait(
    registry: PoolRegistry, model: str, waited: float, deadline: float
) -> float | None:
    """Seconds to wait for a paused key serving ``model``, or None to give up.

    Only when ``failover_max_wait_seconds`` allows it, the key is back within
    what is left of that allowance, and the wait ends before the loop deadline.
    """
    allowance = get_settings().failover_max_wait_seconds - waited
    if allowance <= 0:
        return None
    available = await registry.next_available_at(model)
    if available is None:
        return None
    # A small margin so the marker has expired when we look again.
    seconds = max(0.05, (available - datetime.now(UTC)).total_seconds() + 0.05)
    if seconds > allowance or time.monotonic() + seconds >= deadline:
        return None
    return seconds


async def capacity_refusal(registry: PoolRegistry, model: str) -> PoolExhaustedError | None:
    """A ready ``pool_exhausted`` refusal when no key can serve ``model`` now.

    Checked before the request is admitted, so a client retrying while the pool
    is empty is answered at once: no admission charge, no session lock, no
    history read. None when a key is available, or will be within
    ``failover_max_wait_seconds`` (the loop then waits for it).
    """
    if await select(registry, model) is not None:
        return None
    start = time.monotonic()
    deadline = start + get_settings().provider_timeout * 2
    if await _short_wait(registry, model, 0.0, deadline) is not None:
        return None
    return await _exhausted(registry, model, "pool_exhausted", [], 0, start)


async def execute_with_failover(
    registry: PoolRegistry,
    model: str,
    messages: list[dict],
    *,
    session: object | None = None,
    rebuild_hook: RebuildHook | None = None,
    first_byte_committed: bool = False,
    observer: Callable[[dict], None] | None = None,
    deadline: float | None = None,
    **params: object,
) -> tuple[AttemptResult, FailoverTrace]:
    """Run the bounded failover loop for one logical request.

    Returns ``(result, trace)`` on success or on a terminal failure (``fatal``,
    or any failure once ``first_byte_committed``); the caller renders
    ``result.error`` when ``not result.ok``. Raises ``PoolExhaustedError``,
    with ``.trace`` attached, when no key is left or the budget is spent.

    The whole loop stops once ``2 * provider_timeout`` seconds have passed.

    ``deadline`` (a ``time.monotonic()`` instant) caps the loop earlier than its own
    bound; a caller that runs the loop for several models passes one shared deadline
    so the whole request stays inside it.

    ``observer``, if given, is called (never awaited, errors ignored) with an
    ``attempt`` event before each provider call and a ``result`` event after it,
    for the live view; it cannot change the outcome.
    """
    hook = rebuild_hook or identity_rebuild
    settings = get_settings()
    retries_left = settings.retry_budget
    max_excluded = settings.retry_budget * 2
    excluded: set[str] = set()
    attempts: list[AttemptRecord] = []
    retry_count = 0
    waited = 0.0
    preferred_provider: Provider | None = None
    start_time = time.monotonic()
    # Bounds the whole loop, so one request cannot hold the session lock for minutes.
    # What the request needs, worked out once: the learning notes use it on every attempt.
    needs = needs_from_params(messages, params)
    total_deadline = start_time + settings.provider_timeout * 2
    if deadline is not None:
        total_deadline = min(total_deadline, deadline)

    while True:
        if time.monotonic() >= total_deadline:
            raise await _exhausted(
                registry, model, "budget_exhausted", attempts, retry_count,
                start_time, None,
            )

        candidate = None
        if len(excluded) <= max_excluded:
            candidate = await select(
                registry, model, excluded=frozenset(excluded), preferred_provider=preferred_provider
            )
        if candidate is None:
            pause = await _short_wait(registry, model, waited, total_deadline)
            if pause is not None:
                # A key is back in a moment: wait rather than refuse. Keys that
                # failed are still held back by their own pause markers.
                await asyncio.sleep(pause)
                waited += pause
                excluded.clear()
                continue
            raise await _exhausted(
                registry, model, "pool_exhausted", attempts, retry_count, start_time
            )

        track(note_attempt(registry.redis, candidate.key_id))
        _notify(observer, {
            "kind": "attempt", "n": len(attempts) + 1, "key_id": candidate.key_id,
            "provider": candidate.provider.value, "model": model,
        })
        attempt_started = time.monotonic()
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

        attempt_ms = int((time.monotonic() - attempt_started) * 1000)
        _notify(observer, {
            "kind": "result", "key_id": candidate.key_id, "provider": candidate.provider.value,
            "model": model, "ok": result.ok,
            "status_code": None if result.ok else result.error.status_code,
            "classification": None if result.ok else classify(result.error).state,
            "cause": None if result.ok else pause_reason(result.error),
            "ms": attempt_ms,
        })

        if result.ok:
            track(record_success(registry.redis, model, candidate.key_id, needs))
            attempts.append(
                AttemptRecord(
                    key_id=candidate.key_id,
                    provider=candidate.provider,
                    classification=None,
                    outcome="success",
                    status_code=None,
                    timestamp=time.monotonic(),
                    model=model,
                    ms=attempt_ms,
                )
            )
            trace = FailoverTrace(
                model=model, attempts=tuple(attempts), terminal="success",
                retry_count=retry_count, start_time=start_time,
                end_time=time.monotonic(),
            )
            return result, trace

        classification = classify(result.error)
        reason = pause_reason(result.error)
        pause_seconds: int | None = None
        connectivity = is_connectivity_error(result.error)
        # Teach the gateway what blocked this attempt, so other requests avoid it.
        track(record_failure(
            registry.redis, model, candidate.key_id, needs,
            result.error, classification.state, result.headers, connectivity,
        ))
        try:
            if classification.state == "transient" and connectivity:
                # We could not reach the provider (DNS, refused). The key and
                # model are fine, so nothing is paused; the loop just rotates.
                pass
            elif classification.state == "transient":
                # Only this model cools down; the key's other models stay
                # routable. The provider's own retry hint wins over the default.
                pause_seconds = retry_after_seconds(result.error.message, result.headers)
                await registry.mark_cooldown(
                    candidate.key_id, model=model, seconds=pause_seconds, reason=reason
                )
            elif classification.state == "exhausted":
                # A spent quota is per key, so the whole key pauses. The pause
                # ends at the provider's reset time when it gives one, otherwise
                # at the key's own reset boundary.
                reset_seconds = get_quota_reset_seconds(result.headers)
                if reset_seconds is None:
                    reset_seconds = retry_after_seconds(result.error.message, result.headers)
                pause_seconds = reset_seconds
                await registry.mark_exhausted(
                    candidate.key_id, seconds=reset_seconds, reason=reason
                )
            elif classification.state == "rejected":
                # The credential itself was refused: suspend it for the operator.
                await registry.suspend(candidate.key_id, operator_id=SYSTEM_OPERATOR_ID)
            elif classification.state == "model_scoped":
                # The key works but not for this model (404, a model-scoped 403, or
                # a quota the provider names per model): skip this model on this
                # key. The key stays active for its other models.
                pause_seconds = 86400  # 24 hours
                quota = model_quota_limit(result.error)
                if quota:
                    # A real allowance that is spent: back when the provider says.
                    pause_seconds = retry_after_seconds(
                        result.error.message, result.headers
                    ) or settings.cooldown_seconds
                await registry.mark_cooldown(
                    candidate.key_id, model=model, seconds=pause_seconds, reason=reason
                )
                if result.error.status_code == 404:
                    # A model the provider no longer lists is gone for every key
                    # of that provider: pause it on all of them now instead of
                    # spending one retry per key to learn the same thing.
                    await _pause_model_for_provider(
                        registry, candidate, model, pause_seconds, reason
                    )
        except KeyNotFoundError:
            pass

        # The provider's own text says which limit was hit. Operator log only,
        # key-shaped strings removed, on one line.
        logger.warning(
            "provider attempt failed key=%s provider=%s model=%s status=%s class=%s "
            "reason=%s pause=%s detail=%s",
            candidate.key_id,
            candidate.provider.value,
            model,
            result.error.status_code,
            classification.state,
            reason,
            "none" if connectivity else _pause_text(classification.state, pause_seconds),
            " ".join(redact(result.error.message or "").split())[:600],
        )

        attempts.append(
            AttemptRecord(
                key_id=candidate.key_id,
                provider=candidate.provider,
                classification=classification.state,
                outcome="failed",
                status_code=result.error.status_code,
                timestamp=time.monotonic(),
                model=model,
                ms=attempt_ms,
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
