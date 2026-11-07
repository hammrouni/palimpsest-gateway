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

## [2.2.0] - 2026-11-07

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

## [2.1.0] - 2026-10-10

### Added
- First public release: OpenAI-compatible `/v1/chat/completions` and
  `/v1/models`, a pool of OpenAI, Anthropic, Google Gemini and OpenRouter keys
  with failover, per-key and per-user caps, session history, an operator API and
  dashboard.

[Unreleased]: https://github.com/hammrouni/palimpsest-gateway/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/hammrouni/palimpsest-gateway/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/hammrouni/palimpsest-gateway/releases/tag/v2.1.0
