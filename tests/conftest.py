"""Shared fixtures: app, fakeredis, and an offline provider stub."""
import os

import pytest
from fakeredis.aioredis import FakeRedis

from app.main import create_app
from app.store.redis_store import RedisStore

# -- Chat-completion provider stub (offline, deterministic) ----------------------
#
# Chat requests go through the failover loop, which needs a routable pool key.
# The suite is fully offline, so chat tests seed one key and stub the adapter.
_FAKE_CHAT_ID = "chatcmpl-fake-test"


class _FakeMessage:
    def __init__(self, content: str):
        self._content = content

    def model_dump(self) -> dict:
        return {"role": "assistant", "content": self._content}


class _FakeUsage:
    def __init__(self, total_tokens: int, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.total_tokens = total_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeBlockingResponse:
    """Minimal stand-in for a litellm ``ModelResponse`` (blocking)."""

    def __init__(self, content: str = "mock reply"):
        self.id = _FAKE_CHAT_ID
        self.choices = [_FakeChoice(content)]
        completion_tokens = len(content.split())
        prompt_tokens = 5
        self.usage = _FakeUsage(
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


class _FakeDelta:
    def __init__(self, content: str | None = None, role: str | None = None):
        self.content = content
        self.role = role


class _FakeStreamChoice:
    def __init__(
        self, content: str | None = None, role: str | None = None, finish_reason: str | None = None
    ):
        self.delta = _FakeDelta(content, role)
        self.finish_reason = finish_reason
        self.index = 0


class _FakeChunk:
    """Minimal stand-in for a litellm streaming chunk."""

    def __init__(
        self, content: str | None = None, role: str | None = None, finish_reason: str | None = None
    ):
        self.id = _FAKE_CHAT_ID
        self.choices = [_FakeStreamChoice(content, role, finish_reason)]

    def model_dump(self) -> dict:
        delta: dict = {}
        if self.choices[0].delta.role is not None:
            delta["role"] = self.choices[0].delta.role
        if self.choices[0].delta.content is not None:
            delta["content"] = self.choices[0].delta.content
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": self.choices[0].finish_reason}
            ],
        }


async def _fake_chunk_stream():
    yield _FakeChunk(content="", role="assistant")
    yield _FakeChunk(content="mock reply")
    yield _FakeChunk(finish_reason="stop")


@pytest.fixture
async def chat_provider_mock(redis, monkeypatch):
    """Seed one routable Pool key for ``gpt-4o-mini`` and stub the single-attempt
    provider adapter so ``/v1/chat/completions`` succeeds offline and deterministically.

    Depends on the requesting test module's own ``redis`` fixture (each chat test
    file defines its own ``FakeRedis`` instance and binds it into ``app.state.store``
    itself) so the seeded key is visible to the ``PoolRegistry`` the endpoint builds
    per-request from that same Redis instance.
    """
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()

    import app.router.failover as failover_module
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.provider.adapter import AttemptResult

    registry = PoolRegistry(redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="test-provider-key")
    )

    # Recorded per call so tests can assert the exact `messages` the provider
    # received, not just what was persisted.
    registry.received_calls = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        registry.received_calls.append(messages)
        if params.get("stream"):
            return AttemptResult(ok=True, response=_fake_chunk_stream())
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    return registry


@pytest.fixture(autouse=True)
def _pool_master_key():
    """Ensure PALIMPSEST_GATE_MASTER_KEY is set for the whole session.

    ``PoolRegistry`` lazily builds a process-wide Fernet SecretsManager on first
    use, which fails without the key.
    """
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
def app():
    """Create test FastAPI app."""
    app = create_app()
    return app


@pytest.fixture
async def fake_redis():
    """Provide FakeRedis for testing (decode_responses=True, matching production)."""
    return FakeRedis(decode_responses=True)


@pytest.fixture
async def store(fake_redis):
    """Provide RedisStore with FakeRedis."""
    return RedisStore(fake_redis)


@pytest.fixture
async def async_client(app, fake_redis):
    """Create async test client."""
    app.state.store = RedisStore(fake_redis)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
