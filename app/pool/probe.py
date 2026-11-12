"""Test a pool key: one tiny real request, so an operator knows it answers.

The check goes through the same adapter as live traffic (same endpoint, same
model mapping, same timeouts), so a pass means a real request would work. It is a
diagnostic, not traffic: nothing is settled, no usage or statistics are written,
and no key or model state changes, even when the provider refuses. The provider
sees one request of a few tokens.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.core.redact import redact
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.provider.adapter import ProviderAttemptError, attempt
from app.router.classify import (
    classify,
    is_connectivity_error,
    model_quota_limit,
    pause_reason,
)

PROBE_PROMPT = "Reply with the single word: ok"
_PROBE_MAX_TOKENS = 16
_PROBE_TIMEOUT_S = 30.0
_REPLY_MAX = 80
# litellm prefixes provider errors with its class names ("litellm.AuthenticationError:
# AuthenticationError: OpenAIException - ..."): noise to an operator.
_LITELLM_NOISE = re.compile(r"^(?:litellm\.\w+:\s*)?(?:\w+Error:\s*)?(?:\w+Exception\s*-\s*)?")
_DETAIL_MAX = 300

# What each verdict means to an operator, and what to do about it.
_ADVICE = {
    "ok": "The key answered a real request.",
    "key_rejected": "The provider refused this key. Check it is correct, active and allowed to use this model.",
    "model_not_found": "The provider does not offer this model to this key. Pick another model or check its name.",
    "no_credits": "The key is valid but this model needs a paid balance or resource package. Try one of the provider’s free models.",
    "rate_limited": "The key works but is being rate-limited right now. Try again shortly.",
    "unreachable": "The gateway could not reach the endpoint. Check the base URL and that the server is running.",
    "timeout": "The provider did not answer in time.",
    "provider_error": "The provider reported a server error. Try again later.",
    "bad_request": "The provider rejected the test request. The model may need different settings.",
    "error": "The provider returned an unexpected error.",
}


# Some providers answer a model that has no paid balance or package with a 429 that
# reads like a rate limit. It is a billing problem, and waiting will not fix it.
_NO_BALANCE = re.compile(
    r"(?i)(insufficient (balance|funds|credits?)|no resource package|please recharge|"
    r"out of credits|top up)"
)


def _verdict(error: ProviderAttemptError) -> str:
    """One word for why the test failed, from the same rules live traffic follows."""
    if is_connectivity_error(error):
        return "unreachable"
    if _NO_BALANCE.search(error.message or ""):
        return "no_credits"
    message = (error.message or "").lower()
    if error.code == "timeout" or "timed out" in message or "timeout" in message:
        return "timeout"
    state = classify(error).state
    reason = pause_reason(error)
    if state == "rejected":
        return "key_rejected"
    if state == "model_scoped":
        quota = model_quota_limit(error)
        if quota == 0:
            return "no_credits"  # the plan has no quota for this model at all
        if quota is not None:
            return "rate_limited"  # this model's own quota, back after the reset
        return "model_not_found"
    if state == "exhausted":
        return "no_credits"
    if state == "transient":
        if error.status_code == 429:
            return "rate_limited"
        if error.status_code is None or reason == "network":
            return "unreachable"
        return "provider_error"
    if state == "fatal" and error.status_code == 400:
        return "bad_request"
    return "error"


def _reply_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return " ".join(str(content or "").split())[:_REPLY_MAX]


def _tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    total = getattr(usage, "total_tokens", None)
    return total if isinstance(total, int) else None


async def probe_key(
    registry: PoolRegistry, key_id: str, model: str | None = None
) -> dict[str, Any]:
    """Send one small request through ``key_id`` and report how it went.

    ``model`` defaults to the key's first model. Raises ``KeyNotFoundError`` for an
    unknown key and ``ValueError`` for a model the key does not serve. A provider
    failure is a result (``ok: false``), never an exception.
    """
    record = await registry.get(key_id)
    if record is None:
        raise KeyNotFoundError(key_id)
    chosen = model or record.models[0]
    if chosen not in record.models:
        raise ValueError("model is not one of this key's models")

    result: dict[str, Any] = {
        "key_id": key_id,
        "provider": record.provider.value,
        "model": chosen,
        "ok": False,
        "verdict": "error",
        "message": _ADVICE["error"],
        "status_code": None,
        "latency_ms": None,
        "reply": None,
        "tokens": None,
        "detail": None,
    }
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(
            attempt(
                registry,
                key_id,
                chosen,
                [{"role": "user", "content": PROBE_PROMPT}],
                max_tokens=_PROBE_MAX_TOKENS,
            ),
            timeout=_PROBE_TIMEOUT_S,
        )
    except TimeoutError:
        result.update(verdict="timeout", message=_ADVICE["timeout"])
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return result
    result["latency_ms"] = int((time.monotonic() - started) * 1000)

    if outcome.ok:
        result.update(
            ok=True,
            verdict="ok",
            message=_ADVICE["ok"],
            reply=_reply_text(outcome.response),
            tokens=_tokens(outcome.response),
        )
        return result

    error = outcome.error
    verdict = _verdict(error)
    result.update(
        verdict=verdict,
        message=_ADVICE[verdict],
        status_code=error.status_code,
        # The provider's own words help (a wrong model name, a billing notice);
        # key-shaped strings are removed and it is shown to operators only.
        detail=" ".join(_LITELLM_NOISE.sub("", redact(error.message or "")).split())[:_DETAIL_MAX]
        or None,
    )
    return result


MAX_MODELS_PER_RUN = 30
_PROBE_CONCURRENCY = 3


async def probe_models(registry: PoolRegistry, key_id: str) -> dict[str, Any]:
    """Test every model of a key (a few at a time) and say which ones work.

    This is how an operator finds out which of a provider's models a key may really
    use: a provider's list names what exists, not what a given account can call
    (a model may need a paid balance, a plan or an allow-list). Each model costs one
    tiny request; a refused one costs nothing. Returns the per-model results in the
    key's own order plus the lists of working and failing models.

    Raises ``KeyNotFoundError`` for an unknown key and ``ValueError`` when the key
    lists more than ``MAX_MODELS_PER_RUN`` models.
    """
    record = await registry.get(key_id)
    if record is None:
        raise KeyNotFoundError(key_id)
    if len(record.models) > MAX_MODELS_PER_RUN:
        raise ValueError(
            f"this key lists {len(record.models)} models; test at most {MAX_MODELS_PER_RUN} at once"
        )

    gate = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(model: str) -> dict[str, Any]:
        async with gate:
            return await probe_key(registry, key_id, model)

    results = await asyncio.gather(*(one(model) for model in record.models))
    return {
        "key_id": key_id,
        "provider": record.provider.value,
        "results": results,
        "working": [r["model"] for r in results if r["ok"]],
        "failing": [r["model"] for r in results if not r["ok"]],
    }
