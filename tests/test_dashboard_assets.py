"""Dashboard assets: security headers, CSP compatibility, config form, JS checks."""
import json
import logging
import pathlib
import re
import shutil
import subprocess

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import TUNABLE_FIELDS, get_settings
from app.main import app

JS = pathlib.Path("app/static/dashboard/app.js")


@pytest.fixture
async def client(fake_redis):
    from app.store.redis_store import RedisStore

    app.state.store = RedisStore(fake_redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# -- dashboard responses ---------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_assets_carry_security_headers(client):
    for path in ("/dashboard/", "/dashboard/app.js", "/dashboard/no-such-file.js"):
        resp = await client.get(path)
        assert resp.headers["cache-control"] == "no-cache", path  # also on the 404
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
        assert "default-src 'self'" in resp.headers["content-security-policy"]
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["x-content-type-options"] == "nosniff"


def test_bundle_is_compatible_with_its_csp():
    """The CSP has no 'unsafe-inline': no inline script/style or style attribute."""
    html = pathlib.Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html and " style=" not in html
    js = JS.read_text(encoding="utf-8")
    assert "innerHTML" not in js and "eval(" not in js
    assert "setAttribute('style'" not in js


def test_missing_bundle_directory_disables_the_dashboard_instead_of_crashing(
    monkeypatch, tmp_path, caplog
):
    import app.api.dashboard as dashboard

    monkeypatch.setattr(dashboard, "DASHBOARD_DIR", tmp_path / "absent")
    local = FastAPI()
    with caplog.at_level(logging.ERROR, logger="palimpsest.dashboard"):
        dashboard.mount_dashboard(local)
    assert not any(getattr(r, "path", "") == dashboard.DASHBOARD_MOUNT_PATH for r in local.routes)
    assert "dashboard bundle missing" in caplog.text


# -- startup re-applies saved config -------------------------------------


@pytest.mark.asyncio
async def test_init_store_reapplies_persisted_overrides(monkeypatch):
    import redis.asyncio as aioredis

    from app.main import init_store
    from app.store.config import ConfigStore

    fake = FakeRedis(decode_responses=True)
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: fake)
    await ConfigStore(fake).set_overrides({"cooldown_seconds": 97}, operator_id="op")

    settings = get_settings()
    before = settings.cooldown_seconds
    try:
        await init_store(FastAPI())
        assert get_settings().cooldown_seconds == 97
    finally:
        settings.cooldown_seconds = before


# -- the form mirrors the server allowlist; payload building ----------------------


def test_config_form_fields_match_the_servers_tunable_allowlist():
    js = JS.read_text(encoding="utf-8")
    block = js[js.index("const CONFIG_FIELDS = [") : js.index("\n];", js.index("const CONFIG_FIELDS"))]
    assert set(re.findall(r"\{ name: '([a-z_]+)'", block)) == set(TUNABLE_FIELDS)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_read_config_form_sends_only_changed_fields_and_keeps_text_as_text():
    js = JS.read_text(encoding="utf-8")
    fields = js[js.index("const CONFIG_FIELDS = [") : js.index("\n];", js.index("const CONFIG_FIELDS")) + 3]
    start = js.index("function readConfigForm(")
    reader = js[start : js.index("\n}\n", start) + 3]
    script = fields + reader + """
const make = (vals) => ({
  elements: { namedItem: (name) => {
    const v = vals[name];
    return typeof v === 'boolean' ? { checked: v, value: '' } : { checked: false, value: v === undefined ? '' : String(v) };
  } },
});
const baseline = { verbatim_turns_n: 4, retry_budget: 3, pool_low_water_mark: 0,
  user_daily_cap: 50, request_log_prompts: false, cooldown_seconds: 60,
  failover_event_log_size: 1000, request_log_ttl_hours: 24 };
const same = { ...baseline };
const out = {
  untouched: readConfigForm(make(same), baseline),
  clearCap: readConfigForm(make({ ...same, user_daily_cap: '' }), baseline),
  noCapStaysNoCap: readConfigForm(make({ ...same, user_daily_cap: '' }), { ...baseline, user_daily_cap: null }),
  toggle: readConfigForm(make({ ...same, request_log_prompts: true }), baseline),
  number: readConfigForm(make({ ...same, retry_budget: '5' }), baseline),
};
console.log(JSON.stringify(out));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["untouched"] == {}
    assert out["clearCap"] == {"user_daily_cap": None}
    assert out["noCapStaysNoCap"] == {}
    assert out["toggle"] == {"request_log_prompts": True}
    assert out["number"] == {"retry_budget": 5}


def test_js_passes_a_syntax_check():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    result = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_signed_out_guards_are_in_place():
    """Regression guards for the sign-out race and the 'Bearer null' call."""
    js = JS.read_text(encoding="utf-8")
    route = js[js.index("function renderRoute()") : js.index("function showLogin(")]
    assert "els.app.hidden" in route  # no section mounts while signed out
    api = js[js.index("async function apiFetch(") : js.index("Authorization: 'Bearer ' + key")]
    assert "if (!key)" in api  # never sends "Bearer null"
    boot = js[js.index("async function boot()") :]
    assert "try {" in boot and "catch" in boot  # an unreachable gateway is not a blank page
    create = js[js.index("showIssuedKey(created);") :]
    assert create.index("showIssuedKey(created);") < create.index("await loadAccounts();")


def test_every_page_uses_the_shared_frame_and_in_page_dialogs():
    """Actions sit in the same header slot on every page; forms and confirmations
    open in the shared dialog rather than below the fold or as browser pop-ups."""
    js = JS.read_text(encoding="utf-8")
    sections = js[js.index("const SECTIONS = [") : js.index("\n];", js.index("const SECTIONS"))]
    insights = (JS.parent / "insights.js").read_text(encoding="utf-8")
    assert (js + insights).count("mountPage(view, {\n") == sections.count("render: ")
    assert "window.confirm" not in js.replace("replacement for window.confirm", "")
    assert "alert(" not in js
    html = (JS.parent / "index.html").read_text(encoding="utf-8")
    assert '<dialog id="modal"' in html
