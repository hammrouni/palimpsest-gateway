"""Auto-improve: the gateway learns from every provider answer.

The built-in model table and the provider's rate-limit headers are a starting
point. Every attempt then teaches the gateway something, kept in Redis, so the
next request (anyone's) avoids what blocked this one:

* **Context window.** A success at N tokens proves the window holds N; a
  context-length refusal at N says it does not. Supermode then skips the model
  for prompts that would not fit, before any provider is called.
* **Vision and tools.** A request with images or ``tools`` that succeeds proves
  the capability; one refused for it marks the model as lacking it.
* **Reliability.** Successes and provider-side failures per model; a model that
  fails often, or was just throttled, ranks lower in supermode.
* **Requests per minute, per key.** When a key is throttled (429) and its
  provider sent no rate-limit headers, the number of requests it took that
  minute becomes its learned limit. Keys at their learned limit are tried last,
  so other requests go to keys with room instead of hitting the same 429. A
  later minute that goes past the limit without a 429 raises it again.

Everything here is best effort: a Redis fault is logged and never fails a request.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from app.pool.limits import rate_limit_headers
from app.router.classify import classify

logger = logging.getLogger("palimpsest.learning")

_MODEL_KEY = "pgw:learn:model:{model}"
_KEY_MINUTE = "pgw:learn:key:{key_id}:minute:{minute}"
_KEY_RPM = "pgw:learn:key:{key_id}:rpm"
_MODEL_TTL = 30 * 86400
_RPM_TTL = 86400
_MINUTE_TTL = 180
# Halve the reliability counters past this many, so old history fades.
_DECAY_AT = 200
_RECENT_THROTTLE_SECONDS = 120
# How long a model that refused a request another model then served ranks lower.
_RECENT_REFUSAL_SECONDS = 1800

# Room kept for the reply when the client sets no max_tokens.
_DEFAULT_REPLY_TOKENS = 1024
# A rough chars-per-token ratio; errs high so a borderline prompt picks a bigger window.
_CHARS_PER_TOKEN = 3.5
_IMAGE_TOKENS = 1500
_IMAGE_TYPES = ("image_url", "input_image", "image")

_TOOLS_REFUSED_RE = re.compile(
    r"(?i)(tool|function)[\s_-]*(call(ing)?|use)?[^.]{0,60}"
    r"(not supported|unsupported|does not support|is not available|not enabled)"
    r"|does not support (tools|function)"
)


@dataclass(frozen=True)
class Needs:
    """What one request asks of the model that serves it."""

    tokens: int
    vision: bool
    tools: bool


def _is_image(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") in _IMAGE_TYPES


def _without_images(message: object) -> tuple[object, int]:
    """The message with image parts removed, and how many there were."""
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message, 0
    parts = message["content"]
    kept = [part for part in parts if not _is_image(part)]
    return {**message, "content": kept}, len(parts) - len(kept)


def needs_from(
    messages: list[dict],
    tools: object = None,
    max_tokens: int | None = None,
) -> Needs:
    """Estimated prompt plus reply tokens, and the capabilities the request uses.

    An image counts as a flat ``_IMAGE_TOKENS``: its base64 text says nothing
    about what a provider charges for it.
    """
    stripped = [_without_images(m) for m in messages]
    images = sum(count for _, count in stripped)
    text = json.dumps([m for m, _ in stripped], default=str)
    if tools:
        text += json.dumps(tools, default=str)
    return Needs(
        tokens=math.ceil(len(text) / _CHARS_PER_TOKEN)
        + images * _IMAGE_TOKENS
        + (max_tokens or _DEFAULT_REPLY_TOKENS),
        vision=images > 0,
        tools=bool(tools),
    )


def needs_from_params(messages: list[dict], params: dict[str, Any]) -> Needs:
    max_tokens = params.get("max_completion_tokens") or params.get("max_tokens")
    return needs_from(messages, params.get("tools"), max_tokens if isinstance(max_tokens, int) else None)


@dataclass(frozen=True)
class Learned:
    """What the gateway has learned about one model."""

    ctx_ok: int | None = None  # largest request (tokens) that succeeded
    ctx_fail: int | None = None  # smallest request refused as too long
    vision: bool | None = None
    tools: bool | None = None
    ok: int = 0
    fail: int = 0
    throttled_at: float | None = None
    declined_at: float | None = None  # refused a request another model then served

    def context(self, base: int | None) -> int | None:
        """The window to plan with: what failed caps it, what succeeded raises it."""
        window = base
        if self.ctx_ok is not None and (window is None or self.ctx_ok > window):
            window = self.ctx_ok
        if self.ctx_fail is not None:
            window = self.ctx_fail - 1 if window is None else min(window, self.ctx_fail - 1)
        return window

    def penalty(self, now: float | None = None) -> int:
        """Quality points taken off for failing often or being throttled just now."""
        points = 0
        total = self.ok + self.fail
        if total >= 5:
            points += round(20 * self.fail / total)
        now = time.time() if now is None else now
        if self.throttled_at is not None and now - self.throttled_at < _RECENT_THROTTLE_SECONDS:
            points += 5
        if self.declined_at is not None and now - self.declined_at < _RECENT_REFUSAL_SECONDS:
            points += 30
        return points


def _int(row: dict, name: str) -> int | None:
    value = row.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _flag(row: dict, name: str) -> bool | None:
    value = row.get(name)
    return None if value is None else value == "1"


def _decode(raw: dict) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


def _learned(row: dict[str, str]) -> Learned:
    throttled = row.get("throttled_at")
    declined = row.get("declined_at")
    return Learned(
        ctx_ok=_int(row, "ctx_ok"),
        ctx_fail=_int(row, "ctx_fail"),
        vision=_flag(row, "vision"),
        tools=_flag(row, "tools"),
        ok=_int(row, "ok") or 0,
        fail=_int(row, "fail") or 0,
        throttled_at=float(throttled) if throttled else None,
        declined_at=float(declined) if declined else None,
    )


async def learned_many(redis: Any, models: list[str]) -> dict[str, Learned]:
    """What is known about each model, in one round trip."""
    if not models:
        return {}
    try:
        pipe = redis.pipeline(transaction=False)
        for model in models:
            pipe.hgetall(_MODEL_KEY.format(model=model))
        rows = await pipe.execute()
    except Exception:
        logger.warning("could not read learned model facts", exc_info=True)
        return {model: Learned() for model in models}
    return {model: _learned(_decode(row or {})) for model, row in zip(models, rows, strict=True)}


def _minute(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // 60)


async def note_attempt(redis: Any, key_id: str) -> None:
    """Count one request sent through ``key_id`` this minute."""
    name = _KEY_MINUTE.format(key_id=key_id, minute=_minute())
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.incr(name)
        pipe.expire(name, _MINUTE_TTL)
        await pipe.execute()
    except Exception:
        logger.warning("could not count key attempt", exc_info=True)


async def keys_at_learned_limit(redis: Any, key_ids: list[str]) -> set[str]:
    """Keys that already sent their learned requests-per-minute this minute."""
    if not key_ids:
        return set()
    minute = _minute()
    try:
        pipe = redis.pipeline(transaction=False)
        for key_id in key_ids:
            pipe.get(_KEY_RPM.format(key_id=key_id))
            pipe.get(_KEY_MINUTE.format(key_id=key_id, minute=minute))
        values = await pipe.execute()
    except Exception:
        logger.warning("could not read learned key limits", exc_info=True)
        return set()
    full: set[str] = set()
    for i, key_id in enumerate(key_ids):
        rpm, sent = values[2 * i], values[2 * i + 1]
        if rpm is not None and sent is not None and int(sent) >= int(rpm):
            full.add(key_id)
    return full


async def learned_key_limits(redis: Any, key_ids: list[str]) -> dict[str, int]:
    """The requests-per-minute limit learned for each key that has one."""
    if not key_ids:
        return {}
    try:
        pipe = redis.pipeline(transaction=False)
        for key_id in key_ids:
            pipe.get(_KEY_RPM.format(key_id=key_id))
        values = await pipe.execute()
    except Exception:
        logger.warning("could not read learned key limits", exc_info=True)
        return {}
    return {
        key_id: int(value) for key_id, value in zip(key_ids, values, strict=True) if value is not None
    }


def capability_refused(error: object) -> str | None:
    """``context``, ``vision`` or ``tools`` when a refusal names the missing capability."""
    classification = classify(error)
    if classification.client_code == "context_length_exceeded":
        return "context"
    if classification.client_code == "unsupported_media_type":
        return "vision"
    if getattr(error, "status_code", None) in (400, 404, 422) and _TOOLS_REFUSED_RE.search(
        getattr(error, "message", "") or ""
    ):
        return "tools"
    return None


def model_refused(error: object) -> str | None:
    """Why a model, not the gateway or the key, turned this request down.

    ``context``, ``vision`` or ``tools`` for a missing capability, ``request``
    for any other refusal of the request itself (an unsupported parameter, an
    upstream "bad request"): another model may well accept it as sent.
    """
    refused = capability_refused(error)
    if refused:
        return refused
    classification = classify(error)
    if classification.state == "fatal" and getattr(error, "status_code", None) in (400, 404, 422):
        return "request"
    return None


async def record_request_refused(redis: Any, models: list[str]) -> None:
    """These models refused a request that a later model served: rank them lower for a while."""
    if not models:
        return
    try:
        pipe = redis.pipeline(transaction=False)
        for model in models:
            name = _MODEL_KEY.format(model=model)
            pipe.hset(name, "declined_at", str(time.time()))
            pipe.hincrby(name, "fail", 1)
            pipe.expire(name, _MODEL_TTL)
        await pipe.execute()
    except Exception:
        logger.warning("could not record refused request", exc_info=True)


async def record_success(redis: Any, model: str, key_id: str, needs: Needs) -> None:
    """A model answered: it holds this many tokens and has what the request asked for."""
    name = _MODEL_KEY.format(model=model)
    try:
        row = _decode(await redis.hgetall(name) or {})
        learned = _learned(row)
        updates: dict[str, str] = {}
        if learned.ctx_ok is None or needs.tokens > learned.ctx_ok:
            updates["ctx_ok"] = str(needs.tokens)
        if learned.ctx_fail is not None and needs.tokens >= learned.ctx_fail:
            # An earlier refusal was below what works now: the estimate was off.
            await redis.hdel(name, "ctx_fail")
        if needs.vision:
            updates["vision"] = "1"
        if needs.tools:
            updates["tools"] = "1"
        pipe = redis.pipeline(transaction=False)
        if updates:
            pipe.hset(name, mapping=updates)
        pipe.hincrby(name, "ok", 1)
        pipe.expire(name, _MODEL_TTL)
        await pipe.execute()
        await _maybe_decay(redis, name, learned.ok + 1, learned.fail)
        await _raise_rpm_if_passed(redis, key_id)
    except Exception:
        logger.warning("could not record model success", exc_info=True)


async def record_failure(
    redis: Any,
    model: str,
    key_id: str,
    needs: Needs,
    error: object,
    state: str,
    headers: dict | None,
    connectivity: bool,
) -> None:
    """A model or key refused: remember why, so others are not sent into the same wall."""
    if connectivity:
        return  # our own network, nothing about the model or key
    name = _MODEL_KEY.format(model=model)
    try:
        refused = capability_refused(error)
        pipe = redis.pipeline(transaction=False)
        if refused == "context":
            row = _decode(await redis.hgetall(name) or {})
            known = _int(row, "ctx_fail")
            if known is None or needs.tokens < known:
                pipe.hset(name, "ctx_fail", str(needs.tokens))
        elif refused == "vision" and needs.vision:
            pipe.hset(name, "vision", "0")
        elif refused == "tools" and needs.tools:
            pipe.hset(name, "tools", "0")
        elif state in ("transient", "exhausted", "model_scoped"):
            pipe.hincrby(name, "fail", 1)
        if getattr(error, "status_code", None) == 429 and state == "transient":
            pipe.hset(name, "throttled_at", str(time.time()))
        pipe.expire(name, _MODEL_TTL)
        await pipe.execute()
        if getattr(error, "status_code", None) == 429 and state == "transient":
            if not rate_limit_headers(headers):
                await _learn_rpm(redis, key_id)
    except Exception:
        logger.warning("could not record model failure", exc_info=True)


async def _maybe_decay(redis: Any, name: str, ok: int, fail: int) -> None:
    if ok + fail <= _DECAY_AT:
        return
    await redis.hset(name, mapping={"ok": str(ok // 2), "fail": str(fail // 2)})


async def _learn_rpm(redis: Any, key_id: str) -> None:
    """A 429 with no headers: what this key sent this minute, minus the refused one, is its limit."""
    sent = await redis.get(_KEY_MINUTE.format(key_id=key_id, minute=_minute()))
    allowed = max(1, int(sent or 1) - 1)
    rpm_key = _KEY_RPM.format(key_id=key_id)
    known = await redis.get(rpm_key)
    if known is None or allowed < int(known):
        await redis.set(rpm_key, str(allowed), ex=_RPM_TTL)
        logger.info("learned rate limit key=%s rpm=%s", key_id, allowed)


async def _raise_rpm_if_passed(redis: Any, key_id: str) -> None:
    """A success past the learned limit proves the limit was too low."""
    rpm_key = _KEY_RPM.format(key_id=key_id)
    known = await redis.get(rpm_key)
    if known is None:
        return
    sent = await redis.get(_KEY_MINUTE.format(key_id=key_id, minute=_minute()))
    if sent is not None and int(sent) > int(known):
        await redis.set(rpm_key, str(int(sent)), ex=_RPM_TTL)
