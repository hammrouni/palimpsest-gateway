"""Runtime configuration overrides set by an operator.

One hash, ``pgw:operator:config``, with JSON-encoded values so types survive
the round trip. Every write is audited: who, what, old value, new value, when.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger("palimpsest.config")


class ConfigStore:
    """Persisted runtime configuration overrides."""

    CONFIG_HASH = "pgw:operator:config"

    def __init__(self, redis: Redis):
        """Initialize the store.

        Args:
            redis: Async Redis client
        """
        self.redis = redis

    def _decode(self, raw: dict[Any, Any]) -> dict[str, Any]:
        """Decode the hash into typed values; unreadable entries are logged and skipped."""
        decoded: dict[str, Any] = {}
        for key, value in raw.items():
            name = key.decode() if isinstance(key, bytes) else key
            text = value.decode() if isinstance(value, bytes) else value
            try:
                decoded[name] = json.loads(text)
            except (TypeError, ValueError):
                logger.warning(
                    json.dumps({"event": "config_override_unreadable", "key": name})
                )
        return decoded

    async def get_overrides(self) -> dict[str, Any]:
        """Every persisted override; empty when nothing was overridden."""
        return self._decode(await self.redis.hgetall(self.CONFIG_HASH))

    async def set_overrides(
        self,
        values: dict[str, Any],
        *,
        operator_id: str,
        previous: dict[str, Any] | None = None,
    ) -> None:
        """Persist overrides and audit what actually changed.

        Args:
            values: The new overrides to store.
            operator_id: Operator the change is attributed to.
            previous: Overrides already in effect, for the audit's old values.
        """
        if not values:
            return

        before = previous or {}
        await self.redis.hset(
            self.CONFIG_HASH,
            mapping={key: json.dumps(value) for key, value in values.items()},
        )

        logger.info(
            json.dumps(
                {
                    "event": "config_changed",
                    "operator_id": operator_id,
                    "ts": datetime.now(UTC).isoformat(),
                    "changes": [
                        {"key": key, "old": before.get(key), "new": value}
                        for key, value in values.items()
                    ],
                }
            )
        )
