"""Structured request logging: one JSON record per request, no prompt text by default."""
import json
import logging
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger("palimpsest.requests")


def log_request(
    request_id: str,
    account_id: str,
    session_id: str,
    model: str,
    status_code: int,
    latency_ms: float,
    stream: bool = False,
    outcome: str = "success",
    messages: list[dict[str, Any]] | None = None,
    chain: list[str] | None = None,
    attempts: int | None = None,
    settle_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """Log one structured request record. Never raises.

    Args:
        request_id: Request ID echoed to the client
        account_id: Authenticated account
        session_id: Session ID, or "-"
        model: Client-requested model string
        status_code: HTTP response code
        latency_ms: Request latency in milliseconds
        stream: Whether the response was streamed
        outcome: Outcome token (success, rejected_404, busy_423,
            exhausted_503, error_5xx, stream_interrupted, ...)
        messages: Current turn's messages, logged only with
            ``request_log_prompts`` on
        chain: Pool key ids attempted, in order (ids only)
        attempts: Number of attempts; defaults to ``len(chain)``
        settle_id: Settlement ID; defaults to ``request_id``
        prompt_tokens: Provider-reported prompt tokens, if known
        completion_tokens: Provider-reported completion tokens, if known
    """
    # A logging fault must never change the client's outcome.
    try:
        settings = get_settings()
        chain = chain or []

        record = {
            "timestamp": time.time(),
            "request_id": request_id,
            "settle_id": settle_id or request_id,
            "account_id": account_id,
            "session_id": session_id,
            "model": model,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "stream": stream,
            "outcome": outcome,
            "chain": chain,
            "attempts": attempts if attempts is not None else len(chain),
            "n": settings.verbatim_turns_n,
        }

        if prompt_tokens is not None and completion_tokens is not None:
            record["prompt_tokens"] = prompt_tokens
            record["completion_tokens"] = completion_tokens
            record["total_tokens"] = prompt_tokens + completion_tokens

        if settings.request_log_prompts and messages:
            record["messages"] = messages

        logger.info(json.dumps(record, default=str))
    except Exception:
        logger.error("request-log sink failed for request_id=%s", request_id, exc_info=True)
