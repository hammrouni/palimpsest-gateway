"""User-realm store: accounts and their API keys."""
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.core.crypto import hash_key, prefix_of
from app.core.ids import new_id


def _is_valid_id(value: str) -> bool:
    """Check that a path-supplied id is a well-formed UUID.

    Guards against crafted values (e.g. "by_hash:<hash>") colliding with
    other key shapes under the same Redis namespace.
    """
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class RedisStore:
    """Redis-backed store for user realm (accounts, API keys).

    Namespacing: pgw:user:*
    - pgw:user:account:{account_id} (HASH)
    - pgw:user:account:{account_id}:keys (SET of key_id, for O(1) listing)
    - pgw:user:api_key:{key_id} (HASH)
    - pgw:user:api_key:by_hash:{key_hash} (STRING → key_id)
    - pgw:user:accounts (SET of account_id, written by create_account — the index
      the operator account list reads instead of scanning the keyspace)
    """

    def __init__(self, redis: Redis):
        """Initialize store.

        Args:
            redis: Async Redis client
        """
        self.redis = redis

    def _decode_dict(self, data: dict[Any, Any]) -> dict[str, Any]:
        """Decode Redis response (handles bytes from fakeredis).

        Args:
            data: Raw Redis response dict

        Returns:
            Decoded string dict
        """
        if not data:
            return {}
        return {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in data.items()
        }

    # Account operations

    async def create_account(self, display_name: str = "") -> dict[str, Any]:
        """Create a new account and issue its first API key.

        Args:
            display_name: Human-readable account name (optional)

        Returns:
            Account dict with first key included
        """
        account_id = new_id()
        now = datetime.now(UTC).isoformat()

        account = {
            "id": account_id,
            "display_name": display_name or f"Account {account_id[:8]}",
            "created_at": now,
            "status": "active",
        }

        await self.redis.hset(
            f"pgw:user:account:{account_id}",
            mapping=account,
        )

        await self.redis.sadd("pgw:user:accounts", account_id)

        # Issue first key immediately
        key_record = await self._create_key(account_id)

        return {
            "id": account_id,
            "display_name": account["display_name"],
            "created_at": now,
            "keys": [key_record],
        }

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        """Get account by ID.

        Args:
            account_id: Account UUID

        Returns:
            Account dict or None if not found
        """
        if not _is_valid_id(account_id):
            return None
        data = await self.redis.hgetall(f"pgw:user:account:{account_id}")
        decoded = self._decode_dict(data)
        return decoded if decoded else None

    async def list_all_accounts(self) -> list[dict[str, Any]]:
        """Every indexed account, oldest first, with ``active_key_count`` added.

        Reads the ``pgw:user:accounts`` index; malformed or deleted members are
        skipped.
        """
        index = await self.redis.smembers("pgw:user:accounts")
        account_ids = [
            member.decode() if isinstance(member, bytes) else member for member in index
        ]
        account_ids = [account_id for account_id in account_ids if _is_valid_id(account_id)]
        if not account_ids:
            return []

        # Three pipelined round trips in total, whatever the account count:
        # account hashes + key-id sets, then every key's status.
        pipe = self.redis.pipeline(transaction=False)
        for account_id in account_ids:
            pipe.hgetall(f"pgw:user:account:{account_id}")
            pipe.smembers(f"pgw:user:account:{account_id}:keys")
        rows = await pipe.execute()

        key_ids_by_account: dict[str, list[str]] = {}
        records: dict[str, dict[str, Any]] = {}
        for i, account_id in enumerate(account_ids):
            account = self._decode_dict(rows[2 * i])
            if not account:
                continue  # already-deleted index member
            records[account_id] = account
            key_ids_by_account[account_id] = [
                k.decode() if isinstance(k, bytes) else k for k in rows[2 * i + 1]
            ]

        all_key_ids = [k for ids in key_ids_by_account.values() for k in ids]
        statuses: dict[str, Any] = {}
        if all_key_ids:
            pipe = self.redis.pipeline(transaction=False)
            for key_id in all_key_ids:
                pipe.hget(f"pgw:user:api_key:{key_id}", "status")
            statuses = dict(zip(all_key_ids, await pipe.execute(), strict=True))

        def _active(key_id: str) -> bool:
            status = statuses.get(key_id)
            return (status.decode() if isinstance(status, bytes) else status) == "active"

        accounts = [
            {**account, "active_key_count": sum(1 for k in key_ids_by_account[a] if _active(k))}
            for a, account in records.items()
        ]
        accounts.sort(key=lambda account: account.get("created_at", ""))
        return accounts

    async def delete_account(self, account_id: str) -> bool:
        """Permanently delete an account and all of its API keys.

        Keys go first and the account record last, so a request racing the delete
        fails authentication instead of finding a half-removed account. Sessions
        and quota counters live in other stores; the caller removes those.

        Returns:
            True if the account existed.
        """
        if await self.get_account(account_id) is None:
            return False

        keys_set = f"pgw:user:account:{account_id}:keys"
        key_ids = [
            raw.decode() if isinstance(raw, bytes) else raw
            for raw in await self.redis.smembers(keys_set)
        ]
        for key_id in key_ids:
            record = await self.get_key(key_id)
            if record and record.get("key_hash"):
                await self.redis.delete(f"pgw:user:api_key:by_hash:{record['key_hash']}")
            await self.redis.delete(f"pgw:user:api_key:{key_id}")
        await self.redis.delete(keys_set)
        await self.redis.delete(f"pgw:user:account:{account_id}")
        await self.redis.srem("pgw:user:accounts", account_id)
        return True

    async def set_account_status(self, account_id: str, status: str) -> dict[str, Any] | None:
        """Set an account's status (``active`` / ``disabled``).

        Auth reads this field through ``account_by_key_hash``, which already
        rejects anything but ``active`` — disabling needs no auth-side change.

        Args:
            account_id: Account UUID.
            status: New status value.

        Returns:
            The updated account, or None if the id is malformed or unknown.
        """
        if status not in ("active", "disabled"):
            raise ValueError(f"invalid account status: {status!r}")
        account = await self.get_account(account_id)
        if account is None:
            return None
        await self.redis.hset(f"pgw:user:account:{account_id}", "status", status)
        return {**account, "status": status}

    async def account_by_key_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Get account by resolving key hash (edge auth lookup).

        Edge path for auth dependency:
        1. Hash bearer token
        2. Lookup pgw:user:api_key:by_hash:{key_hash} → key_id
        3. Get key record
        4. Get account
        5. Validate account.status == "active"

        Args:
            key_hash: SHA-256 hash of raw key (hex)

        Returns:
            Account dict (with the resolved ``key_id`` included) or None if
            key invalid/revoked
        """
        key_id = await self.redis.get(
            f"pgw:user:api_key:by_hash:{key_hash}",
        )
        if not key_id:
            return None

        if isinstance(key_id, bytes):
            key_id = key_id.decode()

        key_record = await self.get_key(key_id)
        if not key_record or key_record.get("status") != "active":
            return None

        owner_id = key_record.get("account_id")
        account = await self.get_account(owner_id) if owner_id else None
        if not account or account.get("status") != "active":
            return None

        account["key_id"] = key_id
        return account

    # Key operations

    async def _create_key(self, account_id: str) -> dict[str, Any]:
        """Create an API key for account (internal; returns raw key).

        Args:
            account_id: Account ID

        Returns:
            Key record with raw key field
        """
        from app.core.crypto import generate_key

        key_id = new_id()
        raw_key = generate_key()
        key_hash = hash_key(raw_key)
        now = datetime.now(UTC).isoformat()

        key_record = {
            "id": key_id,
            "account_id": account_id,
            "prefix": prefix_of(raw_key),
            "key_hash": key_hash,
            "created_at": now,
            "last_used_at": now,
            "status": "active",
        }

        await self.redis.hset(
            f"pgw:user:api_key:{key_id}",
            mapping=key_record,
        )

        await self.redis.set(
            f"pgw:user:api_key:by_hash:{key_hash}",
            key_id,
        )

        await self.redis.sadd(f"pgw:user:account:{account_id}:keys", key_id)

        return {
            "id": key_id,
            "account_id": account_id,
            "prefix": key_record["prefix"],
            "key": raw_key,
            "created_at": now,
        }

    async def get_key(self, key_id: str) -> dict[str, Any] | None:
        """Get key by ID (no raw key returned).

        Args:
            key_id: Key UUID

        Returns:
            Key dict (no raw key field) or None
        """
        if not _is_valid_id(key_id):
            return None
        data = await self.redis.hgetall(f"pgw:user:api_key:{key_id}")
        decoded = self._decode_dict(data)
        return decoded if decoded else None

    async def list_keys(self, account_id: str) -> list[dict[str, Any]]:
        """List all keys for account (prefix only, no raw/hash).

        Args:
            account_id: Account ID

        Returns:
            List of key records (id, prefix, created_at, last_used_at, status)
        """
        if not _is_valid_id(account_id):
            return []

        key_ids = await self.redis.smembers(f"pgw:user:account:{account_id}:keys")

        keys = []
        for key_id in key_ids:
            key_id_str = key_id.decode() if isinstance(key_id, bytes) else key_id
            key_data = await self.redis.hgetall(f"pgw:user:api_key:{key_id_str}")
            decoded_data = self._decode_dict(key_data)
            if decoded_data:
                try:
                    keys.append({
                        "id": decoded_data["id"],
                        "prefix": decoded_data["prefix"],
                        "created_at": decoded_data["created_at"],
                        "last_used_at": decoded_data.get("last_used_at", ""),
                        "status": decoded_data["status"],
                    })
                except KeyError:
                    # One partial record must not 500 the whole account/operator list.
                    logging.getLogger("palimpsest.store").warning(
                        "skipping malformed key record %s", key_id_str
                    )

        return keys

    async def revoke_key(self, key_id: str, account_id: str) -> bool:
        """Revoke a key immediately, scoped to its owning account.

        Sets status to "revoked" and drops the reverse hash-lookup index entry;
        next auth attempt → 401.

        Args:
            key_id: Key UUID
            account_id: Account UUID that must own this key

        Returns:
            True if revoked, False if not found, already revoked, or owned by
            a different account
        """
        key_record = await self.get_key(key_id)
        if not key_record:
            return False

        if key_record.get("account_id") != account_id:
            return False

        if key_record.get("status") == "revoked":
            return False

        await self.redis.hset(
            f"pgw:user:api_key:{key_id}",
            "status",
            "revoked",
        )

        key_hash = key_record.get("key_hash")
        if key_hash:
            await self.redis.delete(f"pgw:user:api_key:by_hash:{key_hash}")

        return True

    async def update_key_last_used(self, key_id: str) -> None:
        """Update last_used_at (best-effort, must not fail request).

        Args:
            key_id: Key UUID
        """
        try:
            record_key = f"pgw:user:api_key:{key_id}"
            # hset on a vanished record would recreate a partial hash (no id/prefix/status).
            if not await self.redis.exists(record_key):
                return
            now = datetime.now(UTC).isoformat()
            await self.redis.hset(
                record_key,
                "last_used_at",
                now,
            )
        except Exception:
            logging.getLogger("palimpsest.store").debug("last_used_at update failed", exc_info=True)
