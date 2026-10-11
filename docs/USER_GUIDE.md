# Palimpsest Gateway: User Guide

Complete configuration and operations reference. For a quick start, see the [README](../README.md).

## Contents

- [Concepts](#concepts)
- [Deployment](#deployment)
- [Configuration reference](#configuration-reference)
- [Runtime configuration](#runtime-configuration)
- [Sessions](#sessions)
- [Provider key pool](#provider-key-pool)
- [Paid and free keys](#paid-and-free-keys)
- [Failover and context reconstruction](#failover-and-context-reconstruction)
- [Accounts and API keys](#accounts-and-api-keys)
- [Errors](#errors)
- [Operator dashboard](#operator-dashboard)
- [Costs, alerts and audit](#costs-alerts-and-audit)
- [Observability](#observability)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)

## Concepts

### Two realms

The gateway splits its surface into two realms, each authenticated by a different key type.

**User realm** (`/v1/...`) is what your application calls. It creates accounts, issues API keys, and serves chat completions. User API keys carry the `pgw_live_` prefix.

**Operator realm** (`/v1/operator/...` and `/operator/v1/...`) is the administrative surface. It manages the provider key pool, user accounts, operator keys and runtime configuration. Operator keys carry the `pgw_opk_` prefix.

The two key types are never interchangeable: a user key cannot reach the operator API, and an operator key is not a user account.

### What clients are not told

The gateway keeps two kinds of information out of its responses to users:

- **Credential material.** A provider key is returned exactly once, in the response that creates it. Every later read returns only a short hint. The same holds for operator keys.
- **Provider detail.** Which provider or key served a request is not part of any user response. Failures are reported as generic messages with a stable `code`, so a client can branch on the code without learning how the pool is built. A few messages do name the model the client itself requested, or say what the client can change (for example a too-long conversation, or image input the model cannot take).

This is about the gateway's own responses. The requested model name and the prompts go to the provider that serves the request, under that provider's terms.

All errors use the OpenAI envelope, so existing OpenAI client error handling works unchanged:

```json
{"error": {"type": "api_error", "message": "Internal error.", "param": null, "code": "internal_error"}}
```

## Deployment

### Docker Compose (recommended)

```bash
docker compose up -d --wait
```

No `.env`, and no manual key generation. On first boot `docker/entrypoint.sh` generates a master key and a local operator key and persists them in the `gateway_secrets` volume. Each secret is resolved in this order, and the order is what makes repeated boots safe:

1. an explicit `PALIMPSEST_*` variable from your environment or `.env` — always wins,
2. the value persisted in the secrets volume from a previous boot,
3. a freshly generated value, written to the volume.

Step 2 is the important one. The master key is the root of trust for every credential in Redis; regenerating it would make the whole pool permanently undecryptable. It is therefore generated exactly once.

Compose starts two services:

| Service | Role |
| :--- | :--- |
| `app` | The gateway API and dashboard on port 8000. |
| `redis` | Sessions, encrypted credentials and settlement state. Not published to the host. |

The app waits for Redis to report healthy before starting, so the first request never hits a half-up dependency. `--wait` blocks until both are healthy.

Read the generated operator key any time:

```bash
docker compose exec app cat /app/.secrets/operator_key
```

### Image targets

The Dockerfile has two targets:

| Target | Use |
| :--- | :--- |
| `runtime` (default) | Production. Non-editable install, no dev dependencies. |
| `dev` | Development. Adds the test/lint toolchain and the test suite, and runs uvicorn with `--reload`. |

```bash
make dev    # builds `dev`, mounts your working tree, reloads on save
make test   # pytest inside the dev container
make lint   # ruff inside the dev container
```

### Verifying a running stack

```bash
curl http://localhost:8000/healthz     # {"status":"ok"}
```

Then sign in to the dashboard with the operator key.

### Common tasks

| Task | Command |
| :--- | :--- |
| Tail logs | `make logs` |
| Container status | `make ps` |
| Shell in the app container | `make shell` |
| Redis console | `docker compose exec redis redis-cli` |
| Stop, keep data | `make down` |
| Change the host port | `PALIMPSEST_PORT=8010 docker compose up -d` |
| Expose on all interfaces | `PALIMPSEST_BIND=0.0.0.0 docker compose up -d` |
| Delete all data and the master key | `make clean` |

### Without Compose

Run Redis yourself, then start the app directly:

```bash
uv sync --frozen --extra dev
export PALIMPSEST_GATE_MASTER_KEY="<fernet key>"
export PALIMPSEST_REDIS_URL="redis://localhost:6379/0"
uv run uvicorn app.main:app --reload
```

See the [Configuration reference](#configuration-reference) for generating the master key.
## Configuration reference

Every setting is read from a `PALIMPSEST_`-prefixed environment variable. Unrecognised variables are ignored.

### Required

| Variable | Description |
| :--- | :--- |
| `PALIMPSEST_GATE_MASTER_KEY` | Fernet key used to encrypt credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Not recoverable** — changing or losing it makes every stored credential permanently undecryptable. |
| `PALIMPSEST_GATE_OPERATOR_KEY` | Bootstrap operator key, seeded idempotently on startup. Generate with `python -c "import secrets; print('pgw_opk_' + secrets.token_urlsafe(32))"`. Optional: with no operator key the Operator API has no way to authenticate until you create one. |

The bootstrap key is only seeded while no *active* operator key exists. It is a recovery path, not a permanent credential: once you have created operator keys through the API, the env value is ignored — except when every key has been revoked, at which point it restores access.

### Provider

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PALIMPSEST_PROVIDER_API_KEY` | *(empty)* | Single-key shortcut. On startup it is added to the pool (encrypted, idempotently) for `PROVIDER_NAME` serving `PROVIDER_MODEL`. Add further keys through the pool API. |
| `PALIMPSEST_PROVIDER_NAME` | `openai` | Provider of the env key: `openai`, `anthropic`, `google` or `openrouter`. An invalid value is logged and the key is not added. |
| `PALIMPSEST_PROVIDER_MODEL` | `gpt-4o-mini` | Model the env key serves. Clients must request this exact string. |
| `PALIMPSEST_PROVIDER_TIMEOUT` | `60.0` | Timeout in seconds for each provider attempt. |
| `PALIMPSEST_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Base URL for the OpenRouter adapter. |
| `PALIMPSEST_OPENAI_BASE_URL` | *(none)* | Override for the OpenAI adapter base URL. |

### Store and HTTP

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PALIMPSEST_REDIS_URL` | `redis://localhost:6379/0` | Store location. Compose sets this to `redis://redis:6379/0`. |
| `PALIMPSEST_ALLOW_SIGNUP` | `true` | Self-service `POST /v1/accounts`. Set `false` on any reachable gateway; account creation then needs an operator key. |
| `PALIMPSEST_CONFIG_REFRESH_SECONDS` | `5` | How often each instance re-reads runtime overrides. `0` disables. |
| `PALIMPSEST_CORS_ORIGINS` | `[]` | JSON array of allowed origins. Empty means no cross-origin access. The dashboard is same-origin and non-browser clients ignore CORS, so nothing extra is needed by default. |

### Sessions

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PALIMPSEST_MAX_SESSION_ID_LENGTH` | `128` | Maximum length of a client-supplied `X-Session-ID`. |
| `PALIMPSEST_AUTO_CREATE_SESSIONS` | `false` | Create a session for an unknown `X-Session-ID` instead of returning `404 session_not_found`. Enable for clients that mint their own conversation IDs. Sessions remain per-account. |
| `PALIMPSEST_USAGE_RETENTION_DAYS` | `90` | Days of per-account, per-model token counters kept for the dashboard's Usage page. 1 to 365. |
| `PALIMPSEST_USAGE_RECENT_MAX` | `500` | Recent-request records kept for the dashboard's Requests page. 10 to 10000. |
| `PALIMPSEST_SESSION_LOCK_WAIT_MS` | `2000` | Total wait budget for a session turn lock before rejecting. |
| `PALIMPSEST_SESSION_LOCK_POLL_MS` | `100` | Lock acquisition poll interval. |
| `PALIMPSEST_VERBATIM_TURNS_N` | `4` | Recent turns kept verbatim in the failover context. Minimum 1. |
| `PALIMPSEST_REQUEST_LOG_PROMPTS` | `false` | Log the current turn's prompts. Stored history is never logged. |

### Failover

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PALIMPSEST_RETRY_BUDGET` | `3` | Retries after the first attempt, so a request makes at most `RETRY_BUDGET + 1` provider calls. `0` disables failover (env only; the runtime API accepts 1–20). Spending it returns `budget_exhausted`. |
| `PALIMPSEST_COOLDOWN_SECONDS` | `60` | How long a key rests after a transient provider failure. Minimum 1. |

### Settlement, fairness and pool visibility

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PALIMPSEST_USER_DAILY_CAP` | *(none)* | Per-user daily request cap. Unset = no per-user cap; the pool layer still applies. |
| `PALIMPSEST_POOL_LOW_WATER_MARK` | `0` | Throttle admission when fewer than this many keys are usable. `0` disables the check. |
| `PALIMPSEST_REQUEST_LOG_TTL_HOURS` | `24` | Lifetime of a settlement record. Minimum 1, maximum 8760. |
| `PALIMPSEST_SETTLE_ID_TTL_SECONDS` | `300` | Settlement idempotency marker TTL. Must exceed the longest plausible request (`PROVIDER_TIMEOUT × RETRY_BUDGET`) so a long streaming settlement is never mistaken for a duplicate. |
| `PALIMPSEST_FAILOVER_EVENT_LOG_SIZE` | `1000` | Bound on the failover-events mirror list. Minimum 1, maximum 1000000. |

## Runtime configuration

The settings marked *tunable* below can be changed without a restart, via `PATCH /operator/v1/config`. Overrides are persisted in Redis and re-applied on restart. Values are re-validated against the same bounds on the way in and on the way back out, so an out-of-range or mistyped persisted value is ignored rather than applied.

| Setting | Bounds |
| :--- | :--- |
| `verbatim_turns_n` | ≥ 1 |
| `retry_budget` | 1–20 |
| `pool_low_water_mark` | ≥ 0 |
| `user_daily_cap` | ≥ 1, or `null` to clear the cap |
| `request_log_prompts` | boolean |
| `cooldown_seconds` | ≥ 1 |
| `failover_event_log_size` | 1–1000000 |
| `request_log_ttl_hours` | 1–8760 |

Only these names are accepted. Credentials, the master key and the store URL are deliberately *not* runtime-tunable — a dashboard must never be a way to write a credential. An unrecognised key is rejected; a known key sent as `null` is refused, because assigning `null` onto a required setting would break arithmetic at request time.

```bash
curl -X PATCH http://localhost:8000/operator/v1/config \
  -H "Authorization: Bearer $OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"retry_budget": 5, "cooldown_seconds": 120}'
```

## Sessions

Send `X-Session-ID` to bind a request to a conversation:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $USER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: support-ticket-4711" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Summarize this ticket"}]}'
```

The gateway returns the session id in the `X-Session-ID` response header. If you omit it, one is issued and a new conversation begins.

**Continuity.** The server holds history, so later turns in the same session do not need to resend earlier messages — send only the new turn. History is scoped to the authenticated account, so a session id is meaningless to any other account.

**Concurrency.** Turns on one session are serialized by a per-turn lock. A turn that cannot acquire the lock within `SESSION_LOCK_WAIT_MS` is rejected with `423 session_busy` rather than queued indefinitely. Two different sessions for the same account proceed in parallel.

**Isolation.** Session ids are namespaced per account. A client cannot read or continue another account's conversation by guessing its id.

## Provider key pool

Add keys through the operator API. The raw key is accepted once and never returned again — reads give a short hint only.

```bash
curl -X POST http://localhost:8000/v1/operator/pool/keys \
  -H "Authorization: Bearer $OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "provider": "openai",
        "models": ["gpt-4o-mini"],
        "key": "sk-...",
        "daily_cap": 500,
        "reset_period_hours": 24
      }'
```

Supported providers are `openai`, `anthropic`, `google` and `openrouter`. Adding one is a deliberate code change in `app/pool/models.py`.

### Key states

| State | Meaning |
| :--- | :--- |
| `active` | Eligible for selection. |
| `cooldown` | Rested after a transient failure; returns to `active` when `COOLDOWN_SECONDS` elapses. |
| `exhausted` | Out of quota or hard limit; stays out until its reset time or a manual resume. |
| `suspended` | Set by an operator and stays out until resumed. |

### Quotas

`daily_cap` bounds requests per key per `reset_period_hours` (1–720, default 24). Once a key reaches its cap it moves to `exhausted` and is skipped until the window resets. Leave `daily_cap` unset for no key-level cap; the pool-wide checks still apply.

`PALIMPSEST_USER_DAILY_CAP` bounds requests per *user account* per day, independent of which key serves them. This is the fairness layer: it stops one account from consuming the whole pool. `PALIMPSEST_POOL_LOW_WATER_MARK` throttles everyone when the pool itself gets thin.

### Daily caps and resets

A key's reset time is explicit at creation (`reset_at`), and the window advances from there. The gateway does not guess a provider's calendar day. `reset_at` must be in the future and must carry a timezone.

## Paid and free keys

The pool treats every key the same way. What changes behaviour is the cap you set on it.

### Order of selection

For a request, the candidates are the `active` keys that list the requested model string and are not cooling for that model. They are ordered:

1. Capped keys with requests left, most left first. A key whose cap is used up is skipped until its window resets.
2. Uncapped keys.

Inside each group, keys of the provider that served earlier turns of the session come first, then key id order. Selection only reads state; failover changes it.

### Recommended setups

**All keys paid.** Add them with no `daily_cap`, or with a safety cap. Enter prices under **Costs** and set a monthly budget; turn on **Alerts** (pool capacity, error rate, key problems). Failover covers transient rate limits and outages.

**Free-tier keys.** Give every key a `daily_cap` at or below the allowance its provider publishes today, and set `reset_period_hours` and `reset_at` to match the provider's window. Requests then spread over the keys with the most room left, and a key stops being used when its cap is spent.

**Free first, paid as backstop.** Cap the free keys, leave the paid keys uncapped, and list the same model string on both. Free capacity is drained first.

```bash
# free key, capped
curl -X POST http://localhost:8000/v1/operator/pool/keys \
  -H "Authorization: Bearer $OPERATOR_KEY" -H "Content-Type: application/json" \
  -d '{"provider":"google","models":["gemini-2.5-flash"],"key":"...","label":"free","daily_cap":200,"reset_period_hours":24}'

# paid key, same model string, no cap
curl -X POST http://localhost:8000/v1/operator/pool/keys \
  -H "Authorization: Bearer $OPERATOR_KEY" -H "Content-Type: application/json" \
  -d '{"provider":"google","models":["gemini-2.5-flash"],"key":"...","label":"paid"}'
```

The numbers above are examples, not a statement of any provider's allowance.

### Limits of the approach

- Failover needs the **same model string** on the next key. Different model names are different models.
- Caps are what you enter. The gateway does not read a provider's allowance, except the OpenRouter view in the dashboard.
- Only `daily_cap` is enforced. `token_cap` is shown on the dashboard as an indicator and never blocks a key. Tokens from streams whose provider reports none are not counted.
- Requests made with the same key outside the gateway are not counted. The provider's own refusal still marks the key and fails over.
- There are no per-user tiers. All accounts share `PALIMPSEST_USER_DAILY_CAP`; you can override one account until the next UTC midnight. Any account can use any served model.
- With no capacity left, clients get `503 pool_exhausted`.

### Using it responsibly

- Add keys you are entitled to use, for the purpose their terms allow.
- Do not open extra accounts or projects only to gather more free allowance. Providers generally forbid it.
- Check each provider's rules on sharing or reselling access before others use your gateway.
- Free tiers may be logged or used to improve the provider's products. Read the terms before sending private data through them.
- Keep `PALIMPSEST_ALLOW_SIGNUP=false` and set a user daily cap on any gateway others can reach.

## Failover and context reconstruction

When a provider attempt fails, the gateway classifies the error and retries on the next viable candidate, up to `RETRY_BUDGET` retries after the first attempt.

| Provider error | Gateway behaviour |
| :--- | :--- |
| Rate limit | Key enters `cooldown`; try the next candidate. |
| Quota exhausted / payment required | Key enters `exhausted`; try the next candidate. |
| Transient server error | Key enters `cooldown`; try the next candidate. |
| Provider refuses the key (401 / 403) | Key is **suspended** (`last_modified_by: gateway:auth_rejected`); try the next candidate. Resume it from the dashboard once fixed. |
| Bad request or other client error | **Not retryable.** Returned to the client — retrying cannot help. |

Selection prefers a candidate that has remaining quota, so a nearly-exhausted key is used last rather than first.

### Failover context

A replacement provider has no idea what the previous one was talking about. The gateway hands it the recent conversation, taken from server-side session history:

- The client's leading `system` / `developer` messages come first, exactly once.
- The most recent `VERBATIM_TURNS_N` stored exchanges are passed through verbatim, oldest first.
- The client's current turn is appended last: its last user message and everything after it, so an in-progress tool call and its result are kept.
- Nothing else is sent. Turns older than the window are not forwarded, and no summary of them is synthesized.

So the payload is bounded by the window rather than by the conversation length, and the client never has to resend history. When the session has no stored history (or it cannot be read), the replacement provider gets the same messages the first attempt got, so a client that sends its own history keeps it. A request with `"use_history": false` is always retried with exactly the messages it sent.

To give a replacement provider more context, raise `PALIMPSEST_VERBATIM_TURNS_N` (runtime-tunable). The trade-off is a larger payload on every failover.

## Accounts and API keys

```bash
curl -X POST http://localhost:8000/v1/accounts
```

The response contains the account id and its API key. **The key is shown once.** Store it immediately; it cannot be retrieved afterwards, only rotated — issue a new key and revoke the old one.

Issue additional keys per account, or from the operator realm under `/operator/v1/accounts/{account_id}/keys`. Disabling an account blocks its keys immediately without deleting its session history.

`POST /operator/v1/accounts/{account_id}/daily-cap` with `{"daily_cap": 500}` overrides one account's cap until the next UTC midnight, after which it returns to `PALIMPSEST_USER_DAILY_CAP`. It is refused with `409 pool_low_water` while the pool is at or below its low-water mark.

A request the provider never served — no capacity, every attempt failed, unknown session, session busy — does not count against the user's daily cap. A request the provider rejected as malformed (`400`) does.

### Sessions

Users list their own sessions with `GET /v1/sessions` and delete one, with its whole history, with `DELETE /v1/sessions/{session_id}`. A deleted session ID returns `404` on its next use.

A client that resends the whole conversation every turn (most OpenAI SDK clients) can still send `X-Session-ID`: the gateway detects the earlier turns in the request, does not prepend its stored copy, and stores only the new turn.

## Errors

All errors use the OpenAI envelope. Branch on `code`; treat `message` as human-facing prose that may change.

### Authentication and authorization

| Code | Status | Meaning |
| :--- | :--- | :--- |
| `missing_api_key` | 401 | No `Authorization` header supplied. |
| `invalid_api_key` | 401 | Malformed header, or the key is unknown, revoked or disabled. |
| `auth_failed` | 401 | Credential lookup failed. |
| `forbidden` | 403 | Key is valid but not permitted to reach this resource. |
| `operator_access_denied` | 403 | A non-operator key attempted an operator route. |

### Request and resource errors

| Code | Status | Meaning |
| :--- | :--- | :--- |
| `invalid_request` | 422 | Body failed validation. `param` names the offending field. |
| `not_tunable` | 422 | A known-but-not-runtime-tunable setting was sent to `PATCH /operator/v1/config`. |
| `duplicate_key` | 409 | A pool key with the same material already exists. |
| `last_operator_key` | 409 | Refusing to revoke the only active operator key. |
| `pool_low_water` | 409 | A per-account cap override was refused because the pool is at or below its low-water mark. |
| `key_not_found` | 404 | No such pool key or operator key. |
| `account_not_found` | 404 | No such user account. |
| `session_not_found` | 404 | The session id does not belong to this account. |
| `session_busy` | 423 | Could not acquire the session turn lock within the wait budget. |

### Capacity and dependency errors

| Code | Status | Meaning |
| :--- | :--- | :--- |
| `pool_exhausted` | 503 | No key had routable capacity. Retry after the next reset. |
| `budget_exhausted` | 503 | The retry budget ran out before any candidate succeeded. |
| `provider_unavailable` | 503 | The provider call failed non-fatally and could not be recovered. |
| `service_unavailable` | 503 | Redis was unreachable at the admission gate. |
| `pool_unavailable` | 503 | The pool store was unreachable. |

### Server errors

| Code | Status | Meaning |
| :--- | :--- | :--- |
| `store_not_initialized` | 500 | The store was not ready when the request arrived. |
| `internal_error` | 500 | Unhandled fault. Details are logged, never returned. |
| `account_creation_failed`, `key_creation_failed`, `revoke_key_failed`, `list_keys_failed` | 5xx | The corresponding store operation failed. |

### Response headers

| Header | On | Meaning |
| :--- | :--- | :--- |
| `X-Request-ID` | every response | Correlates the response with the request log and settlement record. |
| `X-Session-ID` | chat responses | The session the turn was bound to. |
| `X-Palimpsest-Retry-Count` | failover and exhaustion errors | How many candidates were attempted. |
| `X-Palimpsest-Next-Reset` | `pool_exhausted` | When the next key is expected to recover. |

These carry retry counts and reset times only — never provider names, key hints or key ids.

## Operator dashboard

A bundled web UI for operators. The gateway serves it itself, so there is nothing extra to install or run.

### Opening it

1. Start the stack (`docker compose up -d`, or `uvicorn app.main:app --port 8000`).
2. Browse to **http://localhost:8000/dashboard/** (keep the trailing slash). If you changed `PALIMPSEST_PORT`, use that port.
3. Sign in with an **operator key** (`pgw_opk_...`). A user key is rejected. Get the generated operator key with:

   ```bash
   docker compose exec app cat /app/.secrets/operator_key
   ```

The key is kept in `sessionStorage` for that tab only. **Sign out** (top right) clears it, and closing the tab does too.

### Sections

| Section | What it does | API it uses |
|---|---|---|
| **Overview** | Headline numbers for a period against the previous one: requests, errors, tokens, active users, pool state, derived cost, firing alerts. | `/operator/v1/overview` |
| **Pool Keys** | List provider keys with label, state, request cap, token cap, next reset and last modifier. Filter, sort, add, edit, suspend, resume or remove. Browse a provider's model list when adding a key. Key material is never shown after creation. | `/v1/operator/pool/keys` |
| **Utilization & Failover** | Per-key spend and remaining quota, the provider's rate-limit view of a key, and the recent failover log. | `/v1/operator/pool/utilization`, `/v1/operator/pool/failover-events` |
| **Users** | List and manage accounts: disable, enable, delete, override a daily cap, see one user's trend, models and recent requests. | `/operator/v1/accounts` |
| **Usage** | Tokens by account, provider and model. | `/operator/v1/usage` |
| **Sessions** | A user's sessions, metadata only. | `/operator/v1/accounts/{id}/sessions` |
| **Requests** | Recent requests, metadata only. | `/operator/v1/requests` |
| **Performance** | Latency percentiles, error classes, per-key history, traffic by hour. | `/operator/v1/stats`, `/operator/v1/stats/hourly` |
| **Costs** | Spend by day, model and user from the prices you enter; month to date, projection, budget. | `/operator/v1/costs`, `/operator/v1/pricing` |
| **Alerts** | Alerts firing now, thresholds and the webhook. | `/operator/v1/alerts*` |
| **Audit & Export** | Operator actions, and CSV export of usage, requests and audit. | `/operator/v1/audit`, `/operator/v1/export/*` |
| **System** | Version, uptime, Redis health and memory, retention settings. | `/operator/v1/system` |
| **Configuration** | Edit runtime-tunable settings. Credentials and the master key are deliberately not editable. | `/operator/v1/config` |

Everything the dashboard does is also available through the Operator API, so it can be scripted with curl.

### If it does not load

- **404** at `/dashboard/`: the bundle is missing from the image. See [Troubleshooting](#troubleshooting).
- **Sign-in fails**: you used a user key. Use the operator key.
- **Connection refused from another machine**: the gateway binds `127.0.0.1` by default. Set `PALIMPSEST_BIND=0.0.0.0` behind a proxy; there is no built-in TLS.

## Costs, alerts and audit

### Costs

Prices are per 1M tokens, in one currency you choose. **Nothing is bundled**: a model with no price adds nothing to the totals, and the dashboard reports how many tokens went unpriced. A price matches a model exactly, or by the longest configured prefix (so a dated variant uses the base model's price). An optional monthly budget drives the month-to-date, projection and budget-used figures.

```bash
curl -X PUT http://localhost:8000/operator/v1/pricing \
  -H "Authorization: Bearer $OPERATOR_KEY" -H "Content-Type: application/json" \
  -d '{"currency":"USD","prices":{"gpt-4o-mini":{"input":0.15,"output":0.60}},"monthly_budget":50}'
```

The prices above are placeholders; enter the ones from your own provider invoices. Cost is an estimate from provider-reported token counts. It is not a bill.

A free-tier key still shows a cost if you price its model. Leave free models unpriced if you want the totals to reflect money you actually spend.

### Alerts

Rules are evaluated on demand (the dashboard bell) and every 60 seconds for webhook delivery. A firing alert is sent once an hour.

| Rule | Default | Fires when |
| :--- | :--- | :--- |
| `key_problems` | on | A key is suspended or exhausted, or no key is active. |
| `pool_remaining_pct` | 20 | Capped pool capacity left falls to this percentage. |
| `error_rate_pct` | 25 | The error rate this hour reaches it, with at least `error_rate_min_requests` (20) requests. |
| `user_cap_pct` | 90 | A user has used this share of the daily cap. Needs `PALIMPSEST_USER_DAILY_CAP`. |
| `webhook_url` | none | An `http(s)` URL receiving a JSON POST with the alerts and a `text` summary. |

Use `POST /operator/v1/alerts/test` to check the webhook. The webhook receives alert text only: ids, counts and key labels, never key material.

### Audit trail

Operator actions (pool key changes, account actions, config, pricing, alert rules) are recorded with the operator id, action, target and time. The newest 5000 records are kept. Writing the audit record is best-effort: if it fails, the action still completes and the failure is logged. Key material is never recorded.

### Exports and metrics

`/operator/v1/export/usage.csv`, `requests.csv` and `audit.csv` return CSV. Cells that start like a spreadsheet formula are prefixed to neutralise them. `GET /operator/v1/metrics` returns Prometheus text; authenticate the scraper with an operator bearer token.

## Observability

One structured JSON record per request is written to the `palimpsest.requests` logger:

```json
{"timestamp": "2026-09-04T20:00:00Z", "request_id": "...", "account_id": "...", "session_id": "...", "model": "gpt-4o-mini", "status_code": 200, "latency_ms": 850, "outcome": "success", "chain": ["key_id_1"], "attempts": 1, "n": 4}
```

- `chain` is the ordered list of key ids actually attempted — the most useful field when debugging failover.
- `outcome` distinguishes `success` from rejection and error classes.
- `n` is the verbatim turn count used in the context.

Prompts are excluded unless `REQUEST_LOG_PROMPTS=true`. Stored history is never logged either way.

For pool-level visibility use `GET /v1/operator/pool/utilization` and `GET /v1/operator/pool/failover-events`, or the dashboard.

## Operations

### Health

`GET /healthz` returns `{"status":"ok"}` once the app is serving. In Compose this backs the container healthcheck. `GET /` lists the mounted endpoints.

### Backup and restore

All durable state lives in Redis. The `redis_data` volume holds it. Back the volume up with `docker compose stop app redis` then copy the volume. Restoring requires the **same** `PALIMPSEST_GATE_MASTER_KEY` — credentials restored under a different key cannot be decrypted.

### Rotating the master key

There is no supported rotation path. Decrypt with the old key, re-encrypt with the new one, and swap atomically. Plan for this before you need it; the safe answer is to keep the key in a secret manager with its own rotation story.

### Scaling

The gateway is stateless apart from Redis, so instances scale horizontally. Session locks are held in Redis, so two instances can serve the same session without corrupting history. Admission and settlement run through Redis-side Lua scripts, so quota accounting stays correct across instances. A runtime config change made through one instance reaches the others within `PALIMPSEST_CONFIG_REFRESH_SECONDS` (default 5).

### Upgrading

`docker compose pull && docker compose up -d`.

## Troubleshooting

**Chat requests fail but the dashboard works** — expected without a provider key. Set `PALIMPSEST_PROVIDER_API_KEY`, or add a pool key through the Operator API. Everything else works without one.

**You cannot sign in to the dashboard** — you need an operator key, not a user key. The generated one is at `docker compose exec app cat /app/.secrets/operator_key`; create your own under Accounts.

**Every request returns `pool_exhausted`** — no usable key. Check `GET /v1/operator/pool/utilization`. Keys sitting in `cooldown` or `exhausted` are skipped; resume them or add keys.

**Every request returns `service_unavailable`** — Redis is unreachable. This fails closed by design: the gateway refuses to serve a request it cannot meter.

**`session_busy` under normal load** — concurrent turns on one session. Raise `SESSION_LOCK_WAIT_MS`, or spread load across session ids.

**Failover produces a thin context** — expected behaviour. The replacement provider receives only the last `VERBATIM_TURNS_N` exchanges plus the current turn; turns older than the window are not carried over. Raise `PALIMPSEST_VERBATIM_TURNS_N` if you need a wider window.

**Credentials will not decrypt after a restart** — the master key changed. Restore the previous `PALIMPSEST_GATE_MASTER_KEY`; without it, re-add every pool key.

**Dashboard is a 404** — the static bundle is missing from the image, which logs `dashboard bundle missing` and disables `/dashboard` rather than failing the API. Build the image with `app/` present.
