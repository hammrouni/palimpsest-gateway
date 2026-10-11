"""A stream that fails before its first chunk is a failed attempt, not a broken reply."""
import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult
from app.router.exhaustion import PoolExhaustedError
from app.router.failover import execute_with_failover

MESSAGES = [{"role": "user", "content": "hi"}]


class _Overloaded(Exception):
    status_code = 503


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


async def _key(registry, material):
    return (
        await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key=material))
    ).id


async def _fails_first():
    raise _Overloaded("This model is currently experiencing high demand")
    yield  # pragma: no cover


async def _good():
    from tests.conftest import _FakeChunk

    yield _FakeChunk(content="hello")
    yield _FakeChunk(finish_reason="stop")


async def _breaks_after_content():
    from tests.conftest import _FakeChunk

    yield _FakeChunk(content="partial")
    raise _Overloaded("late failure")


def _script(monkeypatch, streams):
    calls = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        calls.append(key_id)
        return AttemptResult(ok=True, response=streams[len(calls) - 1]())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    return calls


async def _drain(response):
    return [chunk async for chunk in response]


@pytest.mark.asyncio
async def test_failure_before_first_chunk_fails_over_to_the_next_key(registry, monkeypatch):
    first = await _key(registry, "key-a")
    second = await _key(registry, "key-b")
    calls = _script(monkeypatch, [_fails_first, _good])

    result, trace = await execute_with_failover(registry, "m", MESSAGES, stream=True)

    assert result.ok and len(calls) == 2
    assert trace.retry_count == 1
    assert trace.attempts[0].outcome == "failed" and trace.attempts[0].status_code == 503
    assert trace.attempts[0].classification == "transient"
    # The successful stream arrives complete, first chunk included.
    chunks = await _drain(result.response)
    assert [c.choices[0].delta.content for c in chunks] == ["hello", None]
    # The failing key is cooling for this model only.
    assert {first, second} >= set(calls)
    assert await registry.routable_keys(model="m") != []


@pytest.mark.asyncio
async def test_a_single_overloaded_key_becomes_a_clean_503_not_a_broken_stream(
    registry, monkeypatch
):
    await _key(registry, "key-a")
    _script(monkeypatch, [_fails_first])

    with pytest.raises(PoolExhaustedError) as err:
        await execute_with_failover(registry, "m", MESSAGES, stream=True)

    assert err.value.code == "pool_exhausted"
    assert err.value.next_reset is not None  # the cooldown end, so Retry-After is real


@pytest.mark.asyncio
async def test_failure_after_content_began_is_still_a_stream_error(registry, monkeypatch):
    await _key(registry, "key-a")
    await _key(registry, "key-b")
    calls = _script(monkeypatch, [_breaks_after_content, _good])

    result, _trace = await execute_with_failover(registry, "m", MESSAGES, stream=True)

    assert len(calls) == 1  # content had started: no failover, the stream owns the error
    chunks = []
    with pytest.raises(_Overloaded):
        async for chunk in result.response:
            chunks.append(chunk)
    assert chunks[0].choices[0].delta.content == "partial"


@pytest.mark.asyncio
async def test_empty_stream_is_a_success_with_no_chunks(registry, monkeypatch):
    await _key(registry, "key-a")

    async def empty():
        return
        yield  # pragma: no cover

    _script(monkeypatch, [empty])

    result, _ = await execute_with_failover(registry, "m", MESSAGES, stream=True)

    assert result.ok and await _drain(result.response) == []


@pytest.mark.asyncio
async def test_blocking_requests_are_unaffected(registry, monkeypatch):
    await _key(registry, "key-a")
    from tests.conftest import _FakeBlockingResponse

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    result, _ = await execute_with_failover(registry, "m", MESSAGES)

    assert result.ok and result.response.choices
