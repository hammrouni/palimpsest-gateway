"""Request logging: record shape, prompt opt-in, non-disclosure."""
import json
import logging

import pytest

from app.logging.request_logger import log_request


@pytest.mark.asyncio
async def test_request_log_without_prompts(caplog):
    """Logging with REQUEST_LOG_PROMPTS=false (default) → no prompt content."""
    with caplog.at_level(logging.INFO):
        log_request(
            request_id="req123",
            account_id="account1",
            session_id="sess456",
            model="gpt-4o-mini",
            status_code=200,
            latency_ms=42.5,
            stream=False,
            outcome="success",
            messages=[{"role": "user", "content": "SECRET-PROMPT"}],
        )

    # Parse log record. Use the record's own `.message` (the exact string
    # passed to `logger.info(...)`), not `caplog.text` — the latter prepends
    # a "LEVEL logger.name:file:line " preamble that isn't valid JSON, which
    # is what made this assertion fail before (`record_text.split("\n")[-2]`
    # picked up that preamble instead of the JSON payload).
    assert len(caplog.records) > 0
    record_text = caplog.records[-1].message
    record = json.loads(record_text)

    # Verify structure (no prompts by default)
    assert record["request_id"] == "req123"
    assert record["account_id"] == "account1"
    assert record["session_id"] == "sess456"
    assert record["status_code"] == 200
    assert record["outcome"] == "success"
    assert "messages" not in record  # Default: no prompts

    # Canary: prompt never in log
    assert "SECRET-PROMPT" not in record_text


@pytest.mark.asyncio
async def test_retention_window_configurable(caplog):
    """VERBATIM_TURNS_N is configurable from settings."""
    from app.config import get_settings

    settings = get_settings()
    assert settings.verbatim_turns_n == 4  # Default
    assert isinstance(settings.verbatim_turns_n, int)
