# Development Guide

How to run, test, debug and change the gateway. For configuration and operations see the [User Guide](USER_GUIDE.md); for running it as a user see the [README](../README.md).

## Prerequisites

Either path works. You do not need both.

| Path | Needs |
| :--- | :--- |
| Container (recommended) | Docker with Compose v2 |
| Host | Python 3.11+, and a Redis you can point at |

## Run it

### Container with hot reload

```bash
make dev
```

Your working tree is bind-mounted into the container and uvicorn reloads on save. `.env` is read from the mounted tree, so editing it and restarting picks up new configuration.

Other useful targets: `make test`, `make lint`, `make logs`, `make shell`.

### On the host

Nothing generates the master key for you here, so make one and keep it safe:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate

export PALIMPSEST_GATE_MASTER_KEY="<the key you just generated>"
export PALIMPSEST_REDIS_URL="redis://localhost:6379/0"
uv sync --frozen --extra dev                      # or: pip install -e ".[dev]"
uv run uvicorn app.main:app --reload              # or: uvicorn app.main:app --reload
```

Prefer `uv sync --frozen`. `uv.lock` is the dependency set the test suite is proven against and the one the Docker image installs, so using it keeps your environment identical to what CI and the published image run. `pip install -e ".[dev]` resolves fresh and will drift onto newer majors.

`uv sync --frozen` fails if `pyproject.toml` and `uv.lock` disagree. After changing a dependency, refresh the lock with `uv lock` and commit it.

## Tests

The suite is self-contained: `fakeredis` replaces Redis and the provider adapter is mocked, so it needs no network, no Redis, and no API key.

```bash
uv sync --frozen --extra dev
pytest                        # everything (~20s)
pytest tests/test_settlement.py
pytest -k failover             # by name
pytest -x -vv                  # stop at first failure, verbose
```

`asyncio_mode = "auto"` is set, so async tests need no marker.

Useful flags: `-x` stop on first failure, `--lf` rerun last failures, `-k` filter by name, `--tb=short` for compact tracebacks.

### Layout of the suite

| Path | Covers |
| :--- | :--- |
| `tests/test_*.py` | Unit and integration-style tests against the app and its stores |
| `tests/test_dashboard_*.py`, `test_pool_*_dashboard.py` | The dashboard bundle, including its JS via node |

Some tests `skip` if `node` is not on `PATH`; they cover the dashboard's JavaScript.

## Project tour

```
app/
  main.py            app factory, middleware, exception handlers, lifespan
  config.py          Settings (pydantic-settings) + TUNABLE_FIELDS allowlist
  errors.py          GatewayError and the OpenAI error envelope
  api/               HTTP layer: user routes, operator routes, dashboard mount
  pool/              provider key pool: registry, per-key state, quotas
  provider/adapter.py the ONLY place that talks to a provider (litellm)
  router/            selection, error classification, bounded failover
  context/rebuild.py failover context: the verbatim window handed to a replacement
  session/           per-user history store and the per-turn lock
  settlement/        the single settlement point — admission, metering, quota reads
  usage/             token counters, hourly/daily statistics, operator-set prices
  alerts.py          alert rules, evaluation and webhook delivery (background loop)
  audit.py           bounded operator audit trail (best-effort, never records key material)
  static/dashboard/  the operator dashboard (vanilla HTML/JS/CSS)
tests/               the suite
docker/entrypoint.sh generates and persists secrets, then drops privileges
```

### The path of a chat request

Worth reading once — it explains why the modules are shaped the way they are.

1. `api/chat.py` authenticates the user key and resolves or issues the session.
2. The session turn lock is acquired (`session/locks.py`), so two turns on one session cannot interleave.
3. `settlement/admission.py` decides admission and quota, atomically, via a Lua script in Redis.
4. `router/failover.py` runs the bounded attempt loop. Between attempts the rebuild hook (`context/rebuild.py`) prepares the context for the next provider.
5. `provider/adapter.py` makes the actual provider call and classifies the outcome.
6. `settlement/settle.py` meters the request and updates quota, again atomically via Lua.
7. The exchange is appended to session history and the outcome logged.

### Why settlement is its own module

Quota reads and writes must be atomic or they double-count under concurrency, so they run as Redis Lua scripts. Everything that meters a request goes through `app/settlement/`, which is why API routes never settle directly. If you are adding anything that spends quota, it belongs there.

## Debugging

### Logs

Every request writes one structured JSON record to the `palimpsest.requests` logger. Useful loggers:

| Logger | Emits |
| :--- | :--- |
| `palimpsest.requests` | One JSON record per request — latency, outcome, key chain |
| `palimpsest.chat` | Chat route lifecycle |
| `palimpsest.provider` | Provider calls and error classification |
| `palimpsest.settlement` | Admission and metering |
| `palimpsest.session` | Session store and lock |
| `palimpsest.config` | Runtime config overrides applied or rejected |

```bash
make logs
docker compose logs -f app | grep '"outcome": "success"'
```

### Correlating a request

Every response carries `X-Request-ID`, and the same id appears in the request log and the settlement record. Quote it when investigating.

```bash
curl -i http://localhost:8000/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

### Breakpoints

Attach to the running container:

```bash
make shell          # or: docker compose exec app bash
pip install debugpy && python -m debugpy --listen 0.0.0.0:5678 --wait-for-client \
  -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then point your IDE at port 5678. In the host-venv path, just run uvicorn under your debugger.

### Redis

Redis holds sessions, settlement state and encrypted credentials. It is not published to the host, so reach it through the container:

```bash
docker compose exec redis redis-cli
docker compose exec redis redis-cli KEYS 'pgw:*'
```

## Common changes

### Add a setting

1. Add the field to `Settings` in `app/config.py`, with its bound and default.
2. If it should be changeable at runtime without a restart, add it to `TUNABLE_FIELDS` **and** to `ConfigPatch`. Credentials and the master key must never be tunable.
3. Add it to `.env.example` and the table in [USER_GUIDE.md](USER_GUIDE.md).
4. If the dashboard should expose it, add an entry to `CONFIG_FIELDS` in `app/static/dashboard/app.js`.
5. Add or update the config tests in `tests/test_operator_config.py`.

There are tests that keep the docs honest about the tunable set — they will fail if you miss a step.

### Add an endpoint

Routes live in `app/api/`. Operator-realm routes require the operator dependency in `api/operator_deps.py`; user routes use `api/deps.py`. Raise `GatewayError` for anything non-2xx — the handler in `main.py` converts it to the OpenAI envelope, and doing it any other way will leak internals or lose the `X-Request-ID` header.

### Add an operator view

Operator analytics live in `app/api/operator_insights.py` and read from `app/usage/`, `app/alerts.py` and `app/audit.py`. They return metadata only: ids, counts, timings and operator-set prices, never message content or key material. Write operator actions through `audit(...)`, and keep the dashboard in `app/static/dashboard/` (`app.js`, `insights.js`) in step with the endpoint.

### Add a provider

A deliberate code change in `app/pool/models.py` — the `Provider` enum and its adapter in `app/provider/adapter.py`. It is not a config value.

## Before you push

`pytest` and `ruff check app/ tests/` must pass. CI runs both (tests on Python 3.11 and 3.12), checks `uv.lock`, audits dependencies, and builds both image targets and boots the real stack.

Keep to the architecture rules described above: provider calls only in `provider/adapter.py`, metering only in `settlement/`, errors only as `GatewayError`.

## Troubleshooting

**`make dev` starts but the port is taken** — `PALIMPSEST_PORT=8010 make dev`.

**The container is healthy but requests fail** — check whether a provider key is set. Without one the API and dashboard work but chat requests are rejected.

**Changes are not picked up** — the reloader watches `/app/app` only. Changes to `tests/` or `pyproject.toml` need a restart: `make dev` again.

**Port 6379 already in use** — expected. Redis is not published to the host precisely to avoid this. In the host-venv path, point `PALIMPSEST_REDIS_URL` at whichever Redis you run.

**A test fails only in CI** — check the Python version matrix, or the docker job, which boots the real stack. Run `docker compose up -d --wait` locally and check `/healthz` to reproduce the latter.

**`pytest` is slow or hangs** — the suite should take about twenty seconds. A hang usually means a real Redis connection was attempted; the tests expect `fakeredis`.