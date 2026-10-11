"""A key's reset schedule: midnight UTC by default, and every schedule is editable."""
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

BASE = "/v1/operator/pool/keys"
JS = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard" / "app.js"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers(redis):
    return {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}


async def _add(redis, provider, material):
    return await PoolRegistry(redis).create(KeyCreate(provider=provider, models=["m"], key=material))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider", [Provider.OPENAI, Provider.OPENROUTER, Provider.ANTHROPIC, Provider.GOOGLE]
)
async def test_every_provider_renews_at_midnight_utc_by_default(redis, provider):
    record = await _add(redis, provider, f"{provider.value}-material")

    utc = record.reset_at.astimezone(UTC)
    assert (utc.hour, utc.minute, utc.second) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_explicit_reset_time_wins_over_the_provider_default(redis):
    chosen = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=5)
    record = await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-explicit", reset_at=chosen)
    )

    assert record.reset_at == chosen


@pytest.mark.asyncio
async def test_the_schedule_can_be_edited(client, redis, headers):
    key_id = (await _add(redis, Provider.GOOGLE, "g-edit")).id
    target = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)

    resp = await client.patch(
        f"{BASE}/{key_id}",
        headers=headers,
        json={"reset_at": target.isoformat(), "reset_period_hours": 12},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert datetime.fromisoformat(body["reset_at"]) == target
    assert body["reset_period_hours"] == 12
    assert datetime.fromisoformat(body["next_reset_at"]) == target
    # Editing the schedule alone leaves everything else as it was.
    assert body["models"] == ["m"] and body["state"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"reset_at": "2026-10-07T00:00:00"},  # no timezone
        {"reset_at": None},
        {"reset_period_hours": 0},
        {"reset_period_hours": 721},
        {"reset_period_hours": None},
    ],
)
async def test_invalid_schedules_are_refused_and_change_nothing(client, redis, headers, body):
    record = await _add(redis, Provider.OPENAI, "sk-invalid-sched")

    resp = await client.patch(f"{BASE}/{record.id}", headers=headers, json=body)

    assert resp.status_code == 422
    after = (await client.get(f"{BASE}/{record.id}", headers=headers)).json()
    assert datetime.fromisoformat(after["reset_at"]) == record.reset_at
    assert after["reset_period_hours"] == record.reset_period_hours


@pytest.mark.asyncio
async def test_utilization_view_reports_when_the_key_renews(client, redis, headers):
    await _add(redis, Provider.GOOGLE, "g-util")

    (row,) = (await client.get("/v1/operator/pool/utilization", headers=headers)).json()["keys"]

    assert row["next_reset_at"] and row["seconds_to_reset"] > 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_browser_computes_the_next_pacific_midnight():
    js = JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = js.index("function nextMidnightIn(")
    end = js.index("\n}\n", start) + 3
    script = js[start:end] + """
const when = nextMidnightIn('America/Los_Angeles');
const wall = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hourCycle: 'h23',
  hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(when);
console.log(JSON.stringify({ wall, hoursAway: (when.getTime() - Date.now()) / 3600000 }));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["wall"] == "00:00:00"
    assert 0 < out["hoursAway"] <= 25


def test_dashboard_offers_the_schedule_in_both_forms_and_shows_when_keys_renew():
    js = JS.read_text(encoding="utf-8").replace("\r\n", "\n")

    assert js.count("scheduleButtons(resetAt)") == 2  # add form and edit form
    assert "Midnight Pacific" in js
    assert "function renewCell(" in js
    assert "reset_period_hours: Number(period.value)" in js or "changes.reset_period_hours" in js
