"""Master models: send every request to one chosen model.

Two levels, both operator-set from the dashboard:

* ``master_model``: one model for the whole gateway.
* ``provider_master_models``: one model per provider. A request for a model a
  provider's keys serve is sent to that provider's master.

The gateway-wide master wins. A model nobody serves is left alone when only
provider masters are set, since no provider can be named for it.
"""
from __future__ import annotations

from app.config import get_settings
from app.pool.models import KeyState, Provider
from app.pool.registry import PoolRegistry


async def effective_model(registry: PoolRegistry, requested: str) -> str:
    """The model that will actually serve a request for ``requested``."""
    settings = get_settings()
    if settings.master_model:
        return settings.master_model
    masters = settings.provider_master_models
    if not masters:
        return requested

    records = await registry.list()
    serving = registry.serving_models(records)
    for provider in Provider:  # fixed order, so the choice is stable
        master = masters.get(provider.value)
        if master and any(
            r.provider == provider and r.state != KeyState.SUSPENDED and requested in serving[r.id]
            for r in records
        ):
            return master
    return requested
