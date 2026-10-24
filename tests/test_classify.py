"""Provider failure classification."""
import pathlib

import pytest

from app.pool.models import Provider
from app.provider.adapter import ProviderAttemptError
from app.router.classify import classify


def _error(status_code: int | None, message: str = "generic error") -> ProviderAttemptError:
    return ProviderAttemptError(status_code, "some_code", message, Provider.OPENAI)


# -- canonical trigger set ------------------------------------------------------


def test_429_is_transient():
    result = classify(_error(429, "rate limit reached"))
    assert result.state == "transient"
    assert result.client_status == 503
    assert result.client_code == "provider_unavailable"


def test_402_is_exhausted():
    result = classify(_error(402, "payment required"))
    assert result.state == "exhausted"
    assert result.client_status == 429
    assert result.client_code == "rate_limit_exceeded"
    assert result.client_message == "Rate limit exceeded."


def test_400_with_quota_marker_is_exhausted():
    result = classify(_error(400, "Your account has exceeded its quota limit"))
    assert result.state == "exhausted"
    assert result.client_status == 429
    assert result.client_code == "rate_limit_exceeded"
    assert result.client_message == "Rate limit exceeded."


def test_400_without_quota_marker_is_fatal():
    result = classify(_error(400, "invalid content length"))
    assert result.state == "fatal"
    assert result.client_status == 400
    assert result.client_code == "invalid_request"


@pytest.mark.parametrize(
    "message",
    [
        "Cannot read \"image.png\" (this model does not support image input)",
        "This model does not support image input",
        "image input is not supported",
        "vision is not supported for this model",
        "image_url content is not supported",
        "does not support image input",
    ],
)
def test_400_image_rejection_is_fatal_with_actionable_message(message):
    result = classify(_error(400, message))
    assert result.state == "fatal"
    assert result.client_status == 400
    assert result.client_code == "unsupported_media_type"
    assert "image" in result.client_message


def test_400_context_overflow_is_fatal_with_actionable_message():
    result = classify(
        _error(
            400,
            "This endpoint's maximum context length is 262144 tokens. "
            "However, you requested about 266557 tokens",
        )
    )
    assert result.state == "fatal"
    assert result.client_status == 400
    assert result.client_code == "context_length_exceeded"
    assert "too long" in result.client_message


@pytest.mark.parametrize("marker_word", ["quota", "rate limit", "ratelimit", "insufficient", "credit", "billing", "balance"])
def test_400_quota_marker_regex_covers_all_keywords(marker_word):
    result = classify(_error(400, f"error: {marker_word} exceeded"))
    assert result.state == "exhausted"
    assert result.client_status == 429
    assert result.client_code == "rate_limit_exceeded"


# -- 5xx/timeout extension + auth rejections -------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 529])
def test_5xx_is_transient(status):
    result = classify(_error(status, "server error"))
    assert result.state == "transient"
    assert result.client_status == 503
    assert result.client_code == "provider_unavailable"


def test_timeout_none_status_is_transient():
    result = classify(_error(None, "connection timed out"))
    assert result.state == "transient"
    assert result.client_status == 503
    assert result.client_code == "provider_unavailable"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_takes_the_key_out_of_rotation(status):
    """A refused credential is the key's fault, not the request's: rotate, don't fail."""
    result = classify(_error(status, "invalid credentials"))
    assert result.state == "rejected"
    assert result.client_status == 502
    assert result.client_code == "upstream_rejected"


@pytest.mark.parametrize("status", [404])
def test_404_is_model_scoped(status):
    """404 (model not found) should be model-scoped, not suspend the key."""
    result = classify(_error(status, "model not found"))
    assert result.state == "model_scoped"
    assert result.client_status == 502
    assert result.client_code in ("model_not_found", "model_not_allowed")


@pytest.mark.parametrize("status", [422, 409, 413])
def test_other_4xx_is_fatal_upstream_rejected(status):
    result = classify(_error(status, "not found"))
    assert result.state == "fatal"
    assert result.client_status == 502
    assert result.client_code == "upstream_rejected"


@pytest.mark.parametrize("message", [
    "only available on agentic harnesses",
    "model not supported for this key",
    "not available for this model",
    "access denied for this model",
    "per day limit exceeded for model",
])
def test_403_model_scoped_is_cooldown_not_suspended(message):
    """403 with model-scoped message should be model_scoped, not rejected."""
    result = classify(_error(403, message))
    assert result.state == "model_scoped"
    assert result.client_status == 502
    assert result.client_code == "model_not_allowed"


# -- purity + no I/O -------------------------------------------------------------


def test_classify_is_pure():
    error = _error(429, "rate limit reached")
    first = classify(error)
    second = classify(error)
    assert first == second


def test_classify_module_has_no_await_or_store_import():
    import ast

    source = pathlib.Path("app/router/classify.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            raise AssertionError("classify.py contains an await expression")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.store"), "classify.py imports app.store"
                assert not alias.name.startswith("app.api"), "classify.py imports app.api"
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("app.store"):
                raise AssertionError("classify.py imports from app.store")
            if node.module and node.module.startswith("app.api"):
                raise AssertionError("classify.py imports from app.api")
    assert "import app.provider" not in source
