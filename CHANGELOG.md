# Changelog

All notable changes to Palimpsest Gateway are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): a new feature raises the minor
number, a fix raises the patch number, a breaking change raises the major one.

The version lives in three places that must agree: `pyproject.toml`,
`app/__init__.py` and the top entry below. `tests/test_version.py` fails if they
drift. The running version is shown in the dashboard's **System** section and in
`GET /operator/v1/system`.

## [Unreleased]

### Added
- Test all models: the Test dialog can test every model of a key (a tiny request each,
  three at a time, up to 30) and show which ones answer, then **Keep only the working
  models**. A provider's list names what exists, not what an account may call; this
  finds out without the gateway knowing anything provider-specific. `POST
  /v1/operator/pool/keys/{key_id}/test-models`, audited as `pool_key.test_models`.
- Test reports a billing refusal phrased as a 429 ("Insufficient balance", "no
  resource package") as out of credits instead of a rate limit.
- Test button on every pool key: sends one tiny real request through the key (same
  endpoint and model mapping as live traffic) and shows whether it answered, how
  fast, and a plain verdict (key refused, model not found, out of credits,
  rate-limited, endpoint unreachable, timeout...). `POST
  /v1/operator/pool/keys/{key_id}/test`. A test changes no key or model state and
  counts no usage; it is audited as `pool_key.test`.
- Pool keys take their own `base_url` (OpenAI-compatible providers: custom, OpenAI,
  Mistral, Groq, Z.ai, GitHub Models). The Add key and Edit forms show a Base URL
  field, the model picker lists from it, and the pool API accepts it on create and
  `PATCH`. A custom key no longer depends on the single `PALIMPSEST_CUSTOM_BASE_URL`,
  so one gateway can pool several endpoints. URLs with credentials, query strings or
  link-local and cloud-metadata hosts are refused.
- New providers: Z.ai (`zai`, GLM models, including the coding-plan endpoint via
  `base_url`) and GitHub Models (`github`, `publisher/model` names). Both can list
  their models from the dashboard.
- Rate-limit refusals are counted as **throttled**, not as errors. `pool_exhausted`,
  a rate-limited `budget_exhausted` and user-cap refusals no longer raise the error
  rate, lower the success rate or skew latency; the dashboard draws them as their own
  line (Overview, Performance, user details).
- A client that retries before its `Retry-After` is recorded once per account and
  model; further refusals in that window only count as repeats.
- `failover_max_wait_seconds` (env `PALIMPSEST_FAILOVER_MAX_WAIT_SECONDS`, runtime
  0-30, default 0 = off): when every key is paused but one is back within that time,
  the request waits for it instead of answering 429.

### Changed
- An empty pool is refused right after admission, before the session lock and history
  read, and the admission is given back.
- A retry budget spent only on rate or quota limits answers `429 budget_exhausted`
  with `Retry-After` and "All N keys tried for model ... are rate-limited"; other
  spent budgets stay `503`.
- A user-cap `429` now carries `Retry-After` (until the cap resets).
- Docs: `pool_exhausted` is documented as the `429` it already was.
- Dashboard views are much faster on a busy gateway. The pool state read no longer
  scans the whole Redis keyspace once per key (Utilization, Alerts and Overview took
  8-17 s with 40 keys and 20k requests; now under 0.3 s). Analytics summaries group
  counters once instead of rescanning them per user, model and key; Overview reads
  the previous window's totals only and evaluates alerts on the pool view it already
  holds. `redis[hiredis]` adds the C reply parser.
- Dashboard: sections show a loading placeholder instead of a blank panel, scrollbars
  are thin and themed, account names are fetched once per page load, and the
  "Last modified by" column shows a short operator id (full id on hover).
- Landing site: the header logo loads a 64 px image instead of the 512 px one, and
  the hero logo reserves its real aspect ratio.

### Fixed
- Pool Keys: the State, Provider and Search filters had no effect (they were sent to
  an API that ignores them) and every redraw refetched the list.
- Limits dialog: no second provider call after the first one failed, no doubled full
  stop in the sync message, a failed sync no longer hides the dialog, and the table
  refreshes after a sync changes the cap or used count.
- New user and operator keys no longer report a "last used" time before their first
  use; the API returns `null` until then.
- Charts: the last date label is no longer cut off, and it no longer overlaps the
  label before it.
- Costs: a zero amount shows as 0.00 instead of 0.0000.

## [1.1.1] - 2026-11-07

### Changed
- The user guide's failover table, key states, selection order and troubleshooting now
  match model-scoped pauses and master models; README uses plain hyphens.
- Package metadata uses the maintainer contact email.

### Fixed
- Import order in the failover module, so the CI lint step passes.
- Development now follows Git Flow: `develop` is the integration branch, `main` holds
  tagged releases. Documented in `docs/DEVELOPMENT.md` and `CONTRIBUTING.md`; CI also
  runs on `develop`.

## [1.1.0] - 2026-11-07

### Added
- **Providers:** Mistral, Groq and a `custom` OpenAI-compatible provider, with
  `PALIMPSEST_MISTRAL_BASE_URL`, `PALIMPSEST_GROQ_BASE_URL` and
  `PALIMPSEST_CUSTOM_BASE_URL`.
- **Limit sync:** `POST /v1/operator/pool/sync-limits` and
  `POST /v1/operator/pool/keys/{key_id}/sync-limits` read OpenRouter's `GET /key`
  and correct a key's local `daily_cap` and used counter. `?apply=false` reports
  the drift without writing.
- **Master model** (`master_model`): one model that serves every chat request,
  whatever model the client asked for.
- **Per-provider master model** (`provider_master_models`): one master model per
  provider. The gateway-wide master wins when both are set. Masters are picked
  from dropdowns of models the pool serves, and the API refuses a model no key
  serves.
- **Model sharing** (`share_models_by_provider`): every key serves the combined
  model list of its provider, and saving one key's models copies that list to the
  provider's other keys.
- **Pause reasons:** every cooldown or exhaustion records why (`no_credits`,
  `daily_limit`, `rate_limit`, `network`, `provider_error`, `model_unavailable`,
  `auth`) and the dashboard shows the cause and the return time.
- **Dashboard:** an *Unavailable models* list, an ON/OFF strip for the routing
  options on *Utilization & Failover*, selected-model chips with
  select/unselect-shown in the model picker, and master-model dropdowns on
  *Configuration*.
- `CHANGELOG.md` and a test that keeps the version in step everywhere.

### Changed
- A 404 from a provider pauses that model on every key of the provider instead of
  spending one retry per key.
- A DNS or connection failure on our side pauses neither the key nor the model.
- A 429 is classed by its message: a spent quota exhausts the key, a brief limit
  pauses only the model; OpenRouter's reset time is read from the error body when
  headers are missing.
- The app version is defined once (`app/__init__.py`) and `project_version`
  follows it.

### Fixed
- Provider timeouts were passed to litellm as a tuple, which made every upstream
  call fail before sending, put keys into cooldown and returned 429/503 to
  clients.
- A model-scoped 403 or 404 no longer suspends the whole key.
- Models removed from a key no longer linger in the paused-models view, and
  their pause entries are deleted when the key's models are saved.
- asyncio's repeated `socket.send() raised exception` warning is limited to one
  log line a minute.

## [1.0.0] - 2026-10-10

### Added
- First public release: OpenAI-compatible `/v1/chat/completions` and
  `/v1/models`, a pool of OpenAI, Anthropic, Google Gemini and OpenRouter keys
  with failover, per-key and per-user caps, session history, an operator API and
  dashboard.

[Unreleased]: https://github.com/hammrouni/palimpsest-gateway/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/hammrouni/palimpsest-gateway/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/hammrouni/palimpsest-gateway/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/hammrouni/palimpsest-gateway/releases/tag/v1.0.0
