"""Client rendering of pool exhaustion and fatal provider failures."""
from datetime import UTC, datetime, timedelta

from app.router.classify import Classification
from app.router.exhaustion import PoolExhaustedError, render_exhausted, render_fatal


def test_pool_exhausted_renders_503_with_headers():
    next_reset = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    error = PoolExhaustedError("pool_exhausted", next_reset, retry_count=0)

    rendered = render_exhausted(error, model="gpt-4o-mini")

    assert rendered.status_code == 503
    assert rendered.code == "pool_exhausted"
    assert "gpt-4o-mini" in rendered.message
    assert rendered.headers["X-Palimpsest-Next-Reset"] == "2026-08-26T00:00:00+00:00"
    assert rendered.headers["X-Palimpsest-Retry-Count"] == "0"


def test_budget_exhausted_renders_503_distinct_code():
    error = PoolExhaustedError("budget_exhausted", None, retry_count=3)

    rendered = render_exhausted(error, model="gpt-4o-mini")

    assert rendered.status_code == 503
    assert rendered.code == "budget_exhausted"
    assert "budget" in rendered.message.lower()
    assert "X-Palimpsest-Next-Reset" not in rendered.headers
    assert rendered.headers["X-Palimpsest-Retry-Count"] == "3"


def test_pool_exhausted_and_budget_exhausted_codes_are_distinct():
    pool = render_exhausted(PoolExhaustedError("pool_exhausted", None), model="m")
    budget = render_exhausted(PoolExhaustedError("budget_exhausted", None), model="m")
    assert pool.code != budget.code


def test_no_provider_or_key_identity_in_exhausted_render():
    next_reset = datetime.now(UTC) + timedelta(hours=1)
    error = PoolExhaustedError("pool_exhausted", next_reset, retry_count=2)

    rendered = render_exhausted(error, model="gpt-4o-mini")

    canary_terms = ["openai", "anthropic", "google", "openrouter", "sk-", "key_"]
    haystack = rendered.message.lower() + " ".join(rendered.headers.values()).lower()
    for term in canary_terms:
        assert term not in haystack


def test_render_fatal_uses_classifier_fields():
    classification = Classification(
        state="fatal",
        client_status=400,
        client_code="invalid_request",
        client_message="Request was rejected by the upstream; no provider details are disclosed.",
    )

    rendered = render_fatal(classification, retry_count=1)

    assert rendered.status_code == 400
    assert rendered.code == "invalid_request"
    assert rendered.type == "invalid_request_error"
    assert rendered.message == classification.client_message
    assert rendered.headers["X-Palimpsest-Retry-Count"] == "1"


def test_render_fatal_upstream_rejected_error_type():
    classification = Classification(
        state="fatal",
        client_status=502,
        client_code="upstream_rejected",
        client_message="No upstream credential accepted the request.",
    )

    rendered = render_fatal(classification, retry_count=0)

    assert rendered.type == "upstream_error"


def test_fatal_message_never_contains_raw_key_material():
    canary = "sk-abc123secretdef456"
    classification = Classification(
        state="fatal",
        client_status=400,
        client_code="invalid_request",
        client_message="Request was rejected by the upstream; no provider details are disclosed.",
    )
    assert canary not in classification.client_message
    rendered = render_fatal(classification, retry_count=0)
    assert canary not in rendered.message
