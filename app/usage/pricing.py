"""Operator-set model prices and an optional monthly budget; cost is derived.

Prices are per 1M tokens, in one currency the operator picks. Nothing is
bundled: a model without a price simply costs nothing in the totals, and the
dashboard reports how many tokens went unpriced.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

_KEY = "pgw:operator:pricing"


class ModelPrice(BaseModel):
    input: float = Field(ge=0, le=1_000_000)
    output: float = Field(ge=0, le=1_000_000)


class Pricing(BaseModel):
    currency: str = Field(default="USD", min_length=1, max_length=8)
    prices: dict[str, ModelPrice] = Field(default_factory=dict, max_length=500)
    monthly_budget: float | None = Field(default=None, ge=0)


class PricingStore:
    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def get(self) -> Pricing:
        raw = await self.redis.get(_KEY)
        if not raw:
            return Pricing()
        try:
            return Pricing.model_validate_json(raw)
        except ValueError:
            return Pricing()

    async def set(self, pricing: Pricing) -> Pricing:
        await self.redis.set(_KEY, json.dumps(pricing.model_dump()))
        return pricing


def price_for(pricing: Pricing, model: str) -> ModelPrice | None:
    """Exact match first, then the longest configured prefix (dated model variants)."""
    if model in pricing.prices:
        return pricing.prices[model]
    best = None
    for name in pricing.prices:
        if model.startswith(name) and (best is None or len(name) > len(best)):
            best = name
    return pricing.prices[best] if best else None


def cost_of(pricing: Pricing, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Cost in the pricing currency, or ``None`` when the model has no price."""
    price = price_for(pricing, model)
    if price is None:
        return None
    return (prompt_tokens * price.input + completion_tokens * price.output) / 1_000_000
