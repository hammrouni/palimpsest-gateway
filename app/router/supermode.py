"""Supermode: the gateway picks the model.

A request for ``palimpsest/supermode`` is served by the best model the pool can
serve right now, across every provider and key. "Best" is quality first:

1. Every model a non-suspended key serves is a contender.
2. Contenders that cannot take this request are dropped: a context window too
   small for the prompt, no vision for a request with images, no tool calling
   for a request with ``tools``.
3. The rest are ranked by quality tier, highest first. Ties go to the model
   with the most capacity left (uncapped keys, then remaining daily cap).
4. Only models with a key that can serve them now are kept.

The result is an ordered list: the first model serves the request and the
others are tried in turn when every key of the one before is exhausted.

Quality, context window and capabilities come from a built-in table matched on
the model id (``app.router.supermode_profiles``), corrected by what traffic has
taught the gateway (``app.router.learning``): a learned context window, vision
or tool support, and a quality penalty for models that fail often or were just
throttled. ``supermode_quality`` (``SUPERMODE_QUALITY`` in the environment)
overrides the score of any model.
No provider is called to rank: the choice is made from what the gateway knows,
so it costs no latency and no quota.
"""
from __future__ import annotations

import math
from itertools import groupby

from app.config import get_settings
from app.pool.models import KeyState
from app.pool.registry import PoolRegistry
from app.router.learning import Learned, Needs, learned_many, needs_from
from app.router.selection import select_candidates
from app.router.supermode_profiles import ModelProfile, profile_for
from app.schemas.chat import ChatCompletionRequest

SUPERMODE_MODEL = "palimpsest/supermode"


def is_supermode(model: str) -> bool:
    return model.strip().lower() == SUPERMODE_MODEL


def needs_of(request: ChatCompletionRequest) -> Needs:
    """Estimated prompt plus reply tokens, and the capabilities the request uses."""
    return needs_from(
        [m.model_dump(exclude_none=True) for m in request.messages],
        request.tools,
        request.max_completion_tokens or request.max_tokens,
    )


def _with_learned(profile: ModelProfile, learned: Learned) -> ModelProfile:
    """The built-in profile corrected by what traffic has proven."""
    return ModelProfile(
        quality=profile.quality,
        context=learned.context(profile.context),
        vision=profile.vision if learned.vision is None else learned.vision,
        tools=profile.tools if learned.tools is None else learned.tools,
    )


def _fits(profile: ModelProfile, needs: Needs) -> bool:
    if needs.vision and not profile.vision:
        return False
    if needs.tools and profile.tools is False:
        return False
    return profile.context is None or needs.tokens <= profile.context


def _quality(model: str, profile: ModelProfile) -> int:
    overrides = get_settings().supermode_quality
    return overrides.get(model, profile.quality)


def _capacity(candidates: list) -> float:
    """Capacity left for a model: infinite with an uncapped key, else the cap sum."""
    if any(c.remaining_cap is None for c in candidates):
        return math.inf
    return float(sum(c.remaining_cap for c in candidates))


async def rank_models(registry: PoolRegistry, needs: Needs, limit: int | None = None) -> list[str]:
    """Models able to serve ``needs`` now, best first; at most ``limit`` of them."""
    if limit is None:
        limit = get_settings().supermode_fallback_models + 1
    records = await registry.list()
    serving = registry.serving_models(records)
    served = {
        model
        for record in records
        if record.state != KeyState.SUSPENDED
        for model in serving[record.id]
    }
    learned = await learned_many(registry.redis, sorted(served))
    contenders = []
    for model in served:
        facts = learned[model]
        profile = _with_learned(profile_for(model), facts)
        if _fits(profile, needs):
            contenders.append((_quality(model, profile) - facts.penalty(), model))
    contenders.sort(key=lambda item: (-item[0], item[1]))

    ranked: list[str] = []
    for _quality_tier, tier in groupby(contenders, key=lambda item: item[0]):
        scored = []
        for _, model in tier:
            candidates = await select_candidates(registry, model)
            if candidates:
                scored.append((-_capacity(candidates), model))
        ranked.extend(model for _, model in sorted(scored))
        if len(ranked) >= limit:
            break
    return ranked[:limit]
