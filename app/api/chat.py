"""``POST /v1/chat/completions`` with server-side session history.

Each request runs the same steps, streaming or not:

1. admission (per-user cap, pool low-water mark)
2. session binding: a supplied ``X-Session-ID`` must belong to the caller
3. the per-session turn lock, held until the turn is persisted
4. context assembly, then the bounded failover loop
5. persistence, settlement and exactly one request-log record

Only step 5 differs: a blocking turn finishes in the handler, a streaming turn
finishes in the SSE generator once the provider stream ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_current_account, get_store
from app.config import get_settings
from app.context.rebuild import (
    SessionRef,
    _strip_tool_messages,
    first_attempt_messages,
    rebuild,
    turn_to_persist,
)
from app.core.ids import new_id
from app.core.redact import redact
from app.errors import GatewayError
from app.logging.request_logger import log_request
from app.pool.registry import PoolRegistry
from app.pool.tokens import add_tokens
from app.provider.adapter import AttemptResult
from app.router.exhaustion import PoolExhaustedError, render_exhausted, render_fatal
from app.router.failover import FailoverTrace, execute_with_failover
from app.router.rebuild import identity_rebuild
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    generate_chat_id,
)
from app.session.policy import (
    SessionBusy,
    TurnLock,
    acquire_turn_lock,
    release_turn_lock,
)
from app.session.store import SessionStore
from app.settlement import SettlementInput, admit, refund, settle
from app.store.redis_store import RedisStore
from app.usage.store import SERVED_OUTCOMES, UsageStore, track

router = APIRouter(prefix="/v1", tags=["chat"])
logger = logging.getLogger("palimpsest.chat")

_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def valid_session_id(session_id: str) -> bool:
    """Allowed characters, within ``max_session_id_length``."""
    return bool(SESSION_ID_PATTERN.fullmatch(session_id)) and (
        len(session_id) <= get_settings().max_session_id_length
    )


_PROVIDER_PARAMS = (
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "seed",
    "n",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
)


def _provider_kwargs(request: ChatCompletionRequest) -> dict:
    """Non-None generation params to forward to the provider."""
    return {
        key: getattr(request, key)
        for key in _PROVIDER_PARAMS
        if getattr(request, key, None) is not None
    }


def _extract_usage(usage: object) -> tuple[int, int, int]:
    """Prompt/completion/total tokens from a litellm ``Usage`` object.

    Estimates the split only when the provider returned just ``total_tokens``.
    """
    total_tokens = getattr(usage, "total_tokens", 0) or 0
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None or completion_tokens is None:
        prompt_tokens = int(total_tokens * 0.3)
        completion_tokens = total_tokens - prompt_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _extract_assistant_message(response: object) -> tuple[dict, int, int, int]:
    """The assistant message (``tool_calls`` included) and token usage."""
    prompt_tokens, completion_tokens, total_tokens = _extract_usage(response.usage)
    # Drop null provider fields but keep ``content``: tool-call messages carry null.
    assistant_message = {
        k: v
        for k, v in response.choices[0].message.model_dump().items()
        if v is not None
    }
    assistant_message.setdefault("content", None)
    return assistant_message, prompt_tokens, completion_tokens, total_tokens


def _message_dict(message: ChatMessage) -> dict:
    """A request message without unset optional fields; ``content`` is always kept."""
    data = message.model_dump(exclude_none=True)
    data.setdefault("content", None)
    return data


def _client_chunk(chunk: object, model: str, chat_id: str) -> dict:
    """A chunk as the client sees it: gateway id and model, no provider fingerprint."""
    data = chunk.model_dump()
    if "id" in data:
        data["id"] = chat_id
    if "model" in data:
        data["model"] = model
    data.pop("system_fingerprint", None)
    return data


def _delta_parts(delta: object) -> tuple[str, list]:
    """``(content, tool_call_deltas)`` from a streamed delta (object or dict)."""
    if isinstance(delta, dict):
        content, tool_calls = delta.get("content"), delta.get("tool_calls")
    else:
        content = getattr(delta, "content", None)
        tool_calls = getattr(delta, "tool_calls", None)
    return (content if isinstance(content, str) else ""), (tool_calls or [])


def _merge_tool_calls(acc: dict[int, dict], deltas: list) -> None:
    """Fold streamed ``tool_calls`` deltas into complete calls, keyed by ``index``."""
    for delta in deltas:
        d = delta if isinstance(delta, dict) else delta.model_dump(exclude_none=True)
        entry = acc.setdefault(
            d.get("index") or 0,
            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if d.get("id"):
            entry["id"] = d["id"]
        if d.get("type"):
            entry["type"] = d["type"]
        fn = d.get("function") or {}
        entry["function"]["name"] += fn.get("name") or ""
        entry["function"]["arguments"] += fn.get("arguments") or ""


def _describe_stream_error(exc: Exception) -> str:
    """``stream:<ExceptionType>[ <status>]``: enough to diagnose, nothing to leak."""
    status = getattr(exc, "status_code", None)
    return f"stream:{type(exc).__name__}" + (
        f" {status}" if isinstance(status, int) else ""
    )


@dataclass
class _Turn:
    """State shared by one request's helpers: identity, timing and stores."""

    account_id: str
    request_id: str
    settle_id: str
    model: str
    stream: bool
    started: float
    redis: object
    session_store: SessionStore
    session_id: str | None = None
    # Provider-reported usage; None until known (or if never reported).
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Exception type and status when the provider stream broke mid-reply.
    stream_error: str | None = None
    # True once the SSE generator has begun running.
    stream_started: bool = False
    # Milliseconds until the first streamed chunk reached the client.
    first_chunk_ms: int | None = None

    def latency_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def headers(self) -> dict[str, str]:
        headers = {"X-Request-ID": self.request_id}
        if self.session_id:
            headers["X-Session-ID"] = self.session_id
        return headers

    def log(
        self,
        status_code: int,
        outcome: str,
        *,
        trace: FailoverTrace | None = None,
        messages: list[dict] | None = None,
    ) -> None:
        """One request-log record and one usage record per terminal outcome."""
        attempts = trace.attempts if trace is not None else ()
        log_request(
            request_id=self.request_id,
            settle_id=self.settle_id,
            account_id=self.account_id,
            session_id=self.session_id or "-",
            model=self.model,
            status_code=status_code,
            latency_ms=self.latency_ms(),
            stream=self.stream,
            outcome=outcome,
            chain=[a.key_id for a in attempts],
            messages=messages,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )
        track(
            self._record_usage(
                status_code,
                outcome,
                len(attempts),
                [
                    {
                        "status_code": a.status_code,
                        "classification": a.classification,
                        "key_id": a.key_id,
                    }
                    for a in attempts
                    if a.outcome == "failed"
                ],
                next(
                    (a.key_id for a in reversed(attempts) if a.outcome == "success"),
                    None,
                ),
                # The provider that served it, or the last one tried.
                next((a.provider.value for a in reversed(attempts)), None),
            )
        )

    async def _record_usage(
        self,
        status_code: int,
        outcome: str,
        attempts: int,
        failures: list[dict],
        key_id: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Operator statistics: recent requests, day counters, key and session tokens."""
        if self.stream_error:
            failures = [
                *failures,
                {"status_code": None, "classification": self.stream_error},
            ]
        await UsageStore(self.redis).record_request(
            request_id=self.request_id,
            account_id=self.account_id,
            session_id=self.session_id,
            model=self.model,
            status_code=status_code,
            outcome=outcome,
            latency_ms=self.latency_ms(),
            stream=self.stream,
            attempts=attempts,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            failures=failures,
            provider=provider,
            key_id=key_id,
            ttft_ms=self.first_chunk_ms,
        )
        if (
            outcome not in SERVED_OUTCOMES
            or self.prompt_tokens is None
            or self.completion_tokens is None
        ):
            return
        if key_id:
            record = await PoolRegistry(self.redis).get(key_id)
            if record is not None:
                await add_tokens(
                    self.redis, record, self.prompt_tokens + self.completion_tokens
                )
        if self.session_id:
            await self.session_store.add_usage(
                self.account_id,
                self.session_id,
                self.prompt_tokens,
                self.completion_tokens,
                self.model,
            )

    async def settle(
        self, trace: FailoverTrace, status_code: int, *, quiet: bool = False
    ) -> bool:
        """Settle this request; ``quiet`` logs a failure instead of raising.

        Returns False when a quiet settlement failed.
        """
        try:
            await settle(
                SettlementInput(
                    request_id=self.settle_id,
                    account_id=self.account_id,
                    session_id=self.session_id,
                    model=self.model,
                    trace=trace,
                    status_code=status_code,
                    latency_ms=self.latency_ms(),
                    stream=self.stream,
                ),
                redis=self.redis,
            )
            return True
        except Exception:
            if not quiet:
                raise
            logger.error(
                "settlement failed for request_id=%s session_id=%s",
                self.request_id,
                self.session_id,
                exc_info=True,
            )
            return False

    def error(self, err: GatewayError) -> GatewayError:
        """Attach this request's identity headers to an error about to be raised."""
        err.headers = {**(err.headers or {}), **self.headers()}
        return err


def _busy_response(turn: _Turn, busy: SessionBusy) -> JSONResponse:
    return JSONResponse(
        status_code=423,
        content={
            "error": {
                "message": "Session is currently busy. Please retry shortly.",
                "type": "invalid_request_error",
                "code": "session_busy",
                "param": None,
            }
        },
        headers={
            **turn.headers(),
            "X-Palimpsest-Session-Busy": "true",
            "X-Palimpsest-Retry-After": str(busy.retry_after_ms),
        },
    )


def _empty_trace(model: str) -> FailoverTrace:
    """The trace of a request that failed before any provider attempt."""
    return FailoverTrace(model=model, attempts=(), terminal="fatal", retry_count=0)


async def _admit(turn: _Turn) -> None:
    """Admission gate; a rejection costs nothing."""
    try:
        admission = await admit(turn.account_id, redis=turn.redis)
    except Exception as err:  # Redis fault in the gate: fail closed
        logger.error(
            "admission check failed for request_id=%s", turn.request_id, exc_info=True
        )
        turn.log(503, "error_5xx")
        raise turn.error(
            GatewayError(
                message="Service temporarily unavailable.",
                status_code=503,
                error_type="api_error",
                code="service_unavailable",
            )
        ) from err
    if not admission.admitted:
        turn.log(
            429,
            "rejected_user_cap"
            if admission.code == "user_quota_exhausted"
            else "rejected_pool_low_water",
        )
        raise turn.error(
            GatewayError(
                message=admission.message,
                status_code=429,
                error_type="rate_limit_error",
                code=admission.code,
            )
        )


async def _bind_session(turn: _Turn, x_session_id: str | None) -> dict | None:
    """Resolve the turn's session before taking the lock (no writes).

    An ID this account has not claimed is a 404 unless ``auto_create_sessions``
    is on; without an ID a fresh session is issued. Returns the metadata of an
    existing session, else None.
    """
    if not x_session_id:
        turn.session_id = new_id()
        return None

    if not valid_session_id(x_session_id):
        await refund(turn.account_id, redis=turn.redis)
        raise turn.error(
            GatewayError(
                message="Invalid session ID format",
                status_code=422,
                error_type="invalid_request_error",
                code="invalid_request",
            )
        )

    metadata = await turn.session_store.get_metadata(turn.account_id, x_session_id)
    turn.session_id = x_session_id
    if not metadata and get_settings().auto_create_sessions:
        return None  # claimed under the lock in _assemble
    if not metadata:
        await refund(turn.account_id, redis=turn.redis)
        turn.log(404, "rejected_404")
        # The session ID is not echoed for a session the caller does not own.
        raise GatewayError(
            message="Session not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="session_not_found",
            headers={"X-Request-ID": turn.request_id},
        )
    return metadata


async def _assemble(
    turn: _Turn, request: ChatCompletionRequest, metadata: dict | None
) -> tuple[list[dict], list[dict], SessionRef]:
    """Claim the session and build the provider messages (under the lock).

    Returns ``(current_messages, provider_messages, session_ref)``.
    """
    try:
        store = turn.session_store
        if metadata is None:
            metadata = await store.create_or_claim(turn.account_id, turn.session_id)
        else:
            # Another turn may have finished while we waited for the lock.
            metadata = (
                await store.get_metadata(turn.account_id, turn.session_id) or metadata
            )

        current_messages = [_message_dict(m) for m in request.messages]
        exchanges = (
            await store.read_recent(turn.account_id, turn.session_id)
            if request.use_history
            else []
        )
        provider_messages = first_attempt_messages(
            current_messages, exchanges, has_tools=bool(request.tools)
        )
        session_ref = SessionRef(
            account_id=turn.account_id,
            session_id=turn.session_id,
            turn_count=int((metadata or {}).get("turn_count") or 0),
            redis=store.redis,
        )
        return current_messages, provider_messages, session_ref
    except Exception as exc:
        await refund(turn.account_id, redis=turn.redis)
        turn.log(500, "error_5xx")
        raise turn.error(
            GatewayError(
                message="Internal error",
                status_code=500,
                error_type="api_error",
                code="internal_error",
            )
        ) from exc


async def _unexpected_failure(
    turn: _Turn, trace: FailoverTrace | None, exc: Exception
) -> GatewayError:
    """Settle, log and render an unexpected error in the provider phase.

    A trace from a successful provider call is kept so the consumed call is
    still metered against the key that served it.
    """
    trace = trace or _empty_trace(turn.model)
    turn.log(503, "error_5xx", trace=trace)
    await turn.settle(trace, 503, quiet=True)
    logger.error(
        "provider phase failed for request_id=%s", turn.request_id, exc_info=exc
    )
    return turn.error(
        GatewayError(
            message="Provider call failed",
            status_code=503,
            error_type="api_error",
            code="provider_unavailable",
        )
    )


async def _run_failover(
    turn: _Turn,
    request: ChatCompletionRequest,
    provider_messages: list[dict],
    session_ref: SessionRef,
) -> tuple[AttemptResult, FailoverTrace]:
    """Run the failover loop and return a successful attempt.

    Every failure is settled, logged and raised here, before any byte reaches
    the client, so a failed stream is a plain JSON error, never an SSE event.
    """
    extra = {"stream": True} if turn.stream else {}
    trace = None
    has_tools = bool(request.tools)

    if request.use_history and not has_tools:

        async def rebuild_hook(
            messages: list[dict], session: object, attempt_index: int
        ) -> list[dict]:
            rebuilt = await rebuild(messages, session, attempt_index)
            return _strip_tool_messages(rebuilt)
    elif request.use_history:
        rebuild_hook = rebuild
    else:
        rebuild_hook = identity_rebuild

    try:
        result, trace = await execute_with_failover(
            PoolRegistry(turn.redis),
            request.model,
            provider_messages,
            session=session_ref,
            rebuild_hook=rebuild_hook,
            **extra,
            **_provider_kwargs(request),
        )
    except PoolExhaustedError as exc:
        rendered = turn.error(render_exhausted(exc, model=request.model))
        if exc.trace is not None:
            await turn.settle(exc.trace, rendered.status_code, quiet=True)
        turn.log(rendered.status_code, "exhausted_503", trace=exc.trace)
        raise rendered from exc
    except Exception as exc:
        raise await _unexpected_failure(turn, trace, exc) from exc

    if not result.ok:
        classification = trace.classification
        if classification is not None and classification.state == "fatal":
            err = render_fatal(classification, trace.retry_count)
        else:
            err = GatewayError(
                message="Provider call failed",
                status_code=503,
                error_type="api_error",
                code="provider_unavailable",
            )
        turn.error(err)
        await turn.settle(trace, err.status_code, quiet=True)
        turn.log(err.status_code, "error_5xx", trace=trace)
        raise err
    return result, trace


async def _persist(
    turn: _Turn, current_messages: list[dict], assistant_message: dict
) -> bool:
    """Append the turn to session history; False (logged) on failure.

    The provider call is already spent, so a persistence fault must not
    discard the completion.
    """
    try:
        exchange = [*turn_to_persist(current_messages), assistant_message]
        await turn.session_store.append_exchange(
            turn.account_id, turn.session_id, exchange
        )
        await turn.session_store.touch(turn.account_id, turn.session_id)
        return True
    except Exception:
        logger.error(
            "persistence failed for request_id=%s session_id=%s",
            turn.request_id,
            turn.session_id,
            exc_info=True,
        )
        return False


async def _stream_chunks(
    turn: _Turn,
    result: AttemptResult,
    trace: FailoverTrace,
    current_messages: list[dict],
    turn_lock: TurnLock,
) -> AsyncIterator[str]:
    """Relay an established provider stream as SSE.

    A failure while reading chunks is a mid-stream failure: one SSE error
    event, then a stop, with no ``[DONE]`` and no retry. The session lock is
    released however the stream ends, client disconnect included.
    """
    turn.stream_started = True
    chat_id = generate_chat_id()
    finalized = False  # settled and logged; a disconnect must not repeat it
    try:
        content = ""
        tool_calls: dict[int, dict] = {}
        try:
            async for chunk in result.response:
                usage = getattr(chunk, "usage", None)
                if usage:
                    p, c, total = _extract_usage(usage)
                    if total:
                        turn.prompt_tokens, turn.completion_tokens = p, c
                    if not chunk.choices:
                        continue  # usage-only chunk; not forwarded
                if chunk.choices:
                    piece, deltas = _delta_parts(
                        getattr(chunk.choices[0], "delta", None)
                    )
                    content += piece
                    _merge_tool_calls(tool_calls, deltas)
                yield f"data: {json.dumps(_client_chunk(chunk, turn.model, chat_id))}\n\n"
        except Exception as exc:
            # The client gets a generic error; the cause (type and status,
            # never the message) goes to the log and the Requests page.
            turn.stream_error = _describe_stream_error(exc)
            logger.warning(
                "provider stream interrupted request_id=%s cause=%s detail=%s",
                turn.request_id,
                turn.stream_error,
                redact(str(getattr(exc, "message", exc)))[:300],
            )
            finalized = True
            await turn.settle(trace, 200, quiet=True)
            turn.log(200, "stream_interrupted", trace=trace, messages=current_messages)
            error = {
                "error": {
                    "type": "api_error",
                    "message": "Provider stream interrupted.",
                    "code": "provider_stream_interrupted",
                    "param": None,
                }
            }
            yield f"data: {json.dumps(error)}\n\n"
            return

        # [DONE] is final for the client; a later persistence or settlement
        # failure shows only in the logged outcome.
        yield "data: [DONE]\n\n"
        assistant_message: dict = {
            "role": "assistant",
            "content": content or (None if tool_calls else ""),
        }
        if tool_calls:
            assistant_message["tool_calls"] = [
                tool_calls[i] for i in sorted(tool_calls)
            ]
        persisted = await _persist(turn, current_messages, assistant_message)
        # A consumed provider call is always settled, even if persistence failed.
        settled = await turn.settle(trace, 200, quiet=True)

        finalized = True
        turn.log(
            200,
            "success" if persisted and settled else "success_persist_failed",
            trace=trace,
            messages=current_messages if persisted else None,
        )
    except (GeneratorExit, asyncio.CancelledError):
        # Client disconnect after the provider call: still meter and log it.
        # Shielded so a pending cancellation cannot interrupt settlement.
        if not finalized:
            finalized = True
            await asyncio.shield(turn.settle(trace, 200, quiet=True))
            turn.log(200, "stream_interrupted", trace=trace, messages=current_messages)
        raise
    finally:
        await release_turn_lock(turn_lock)


class _TurnStreamingResponse(StreamingResponse):
    """Finalizes the turn however the response ends.

    Starlette never closes the body iterator, so a client that disconnects
    early would leave the generator suspended (or never started) and the
    session lock renewed forever.
    """

    def __init__(self, body: AsyncIterator[str], on_close, **kwargs) -> None:
        super().__init__(body, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_close()


def _stream_response(
    turn: _Turn,
    result: AttemptResult,
    trace: FailoverTrace,
    current_messages: list[dict],
    turn_lock: TurnLock,
) -> StreamingResponse:
    body = _stream_chunks(turn, result, trace, current_messages, turn_lock)

    async def on_close() -> None:
        await body.aclose()  # runs the generator's own cleanup if it is suspended
        if not turn.stream_started:
            await asyncio.shield(turn.settle(trace, 200, quiet=True))
            turn.log(200, "stream_interrupted", trace=trace, messages=current_messages)
            await release_turn_lock(turn_lock)

    return _TurnStreamingResponse(
        body, on_close, media_type="text/event-stream", headers=turn.headers()
    )


async def get_session_store(request: Request) -> SessionStore:
    """Session store bound to the app's Redis client."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return SessionStore(request.app.state.store.redis)


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    summary="Chat Completion",
    description="Create a chat completion request with session binding.",
)
async def chat_completions(
    request: ChatCompletionRequest,
    store: Annotated[RedisStore, Depends(get_store)],
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    account: Annotated[dict, Depends(get_current_account)],
    http_request: Request,
    x_session_id: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
):
    """Create a chat completion (blocking, or SSE when ``stream`` is true).

    Raises:
        GatewayError: 401 (auth), 422 (validation), 404 (foreign session),
            429 (admission), 503 (provider / pool exhausted)
    """
    # A valid client X-Request-ID is echoed but never used as the settlement
    # idempotency key, so a reused ID cannot look like a duplicate settlement.
    request_id = (
        x_request_id
        if x_request_id and _CLIENT_REQUEST_ID.fullmatch(x_request_id)
        else new_id()
    )
    settle_id = new_id() if request_id == x_request_id else request_id
    turn = _Turn(
        account_id=account["id"],
        request_id=request_id,
        settle_id=settle_id,
        model=request.model,
        stream=request.stream,
        started=time.monotonic(),
        redis=store.redis,
        session_store=session_store,
    )

    await _admit(turn)
    metadata = await _bind_session(turn, x_session_id)

    settings = get_settings()
    try:
        turn_lock = await acquire_turn_lock(
            store.redis,
            turn.account_id,
            turn.session_id,
            wait_ms=settings.session_lock_wait_ms,
            poll_ms=settings.session_lock_poll_ms,
        )
    except SessionBusy as busy:
        await refund(turn.account_id, redis=turn.redis)
        turn.log(423, "busy_423")
        return _busy_response(turn, busy)

    handed_to_stream = False
    try:
        current_messages, provider_messages, session_ref = await _assemble(
            turn, request, metadata
        )
        result, trace = await _run_failover(
            turn, request, provider_messages, session_ref
        )

        if turn.stream:
            handed_to_stream = True  # the response owns the lock from here
            return _stream_response(turn, result, trace, current_messages, turn_lock)

        try:
            assistant_message, prompt_tokens, completion_tokens, total_tokens = (
                _extract_assistant_message(result.response)
            )
            if total_tokens:
                turn.prompt_tokens, turn.completion_tokens = (
                    prompt_tokens,
                    completion_tokens,
                )
            finish_reason = (
                getattr(result.response.choices[0], "finish_reason", None) or "stop"
            )
            persisted = await _persist(turn, current_messages, assistant_message)
            response_data = {
                "id": generate_chat_id(),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [
                    {
                        "index": 0,
                        "message": assistant_message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
            # Settle before logging, so a settlement failure yields one
            # accurate failure record rather than a success plus a failure.
            await turn.settle(trace, 200)
        except Exception as exc:
            raise await _unexpected_failure(turn, trace, exc) from exc

        turn.log(
            200,
            "success" if persisted else "success_persist_failed",
            trace=trace,
            messages=current_messages,
        )
        return JSONResponse(content=response_data, headers=turn.headers())
    finally:
        if not handed_to_stream:
            await release_turn_lock(turn_lock)
