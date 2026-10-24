"""When a key's observed rate limit still holds, and when it has lifted."""
from datetime import UTC, datetime, timedelta

from app.pool.limits import quota_blocked_until

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _observed(headers: dict, observed_at: datetime = NOW) -> dict:
    return {"headers": headers, "updated_at": observed_at.isoformat()}


def test_zero_remaining_blocks_until_the_epoch_ms_reset():
    reset = NOW + timedelta(hours=6)
    observed = _observed(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(reset.timestamp() * 1000))}
    )

    assert quota_blocked_until(observed, NOW) == reset


def test_a_reset_in_the_past_does_not_block():
    past = NOW - timedelta(minutes=1)
    observed = _observed(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(past.timestamp() * 1000))}
    )

    assert quota_blocked_until(observed, NOW) is None


def test_remaining_above_zero_does_not_block():
    reset = NOW + timedelta(hours=6)
    observed = _observed(
        {"x-ratelimit-remaining": "3", "x-ratelimit-reset": str(int(reset.timestamp() * 1000))}
    )

    assert quota_blocked_until(observed, NOW) is None


def test_zero_remaining_without_a_reset_time_does_not_block():
    observed = _observed({"x-ratelimit-remaining": "0"})

    assert quota_blocked_until(observed, NOW) is None


def test_relative_reset_counts_from_when_the_headers_were_observed():
    observed_at = NOW - timedelta(seconds=600)
    observed = _observed({"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "3600"}, observed_at)

    assert quota_blocked_until(observed, NOW) == observed_at + timedelta(seconds=3600)


def test_retry_after_lifts_once_it_has_passed():
    observed = _observed({"ratelimit-remaining": "0", "retry-after": "30"})

    assert quota_blocked_until(observed, NOW + timedelta(seconds=10)) == NOW + timedelta(seconds=30)
    assert quota_blocked_until(observed, NOW + timedelta(seconds=31)) is None


def test_missing_or_malformed_observation_does_not_block():
    assert quota_blocked_until(None, NOW) is None
    assert quota_blocked_until({"headers": {"x-ratelimit-remaining": "0"}}, NOW) is None
    assert quota_blocked_until({"headers": "not-a-dict", "updated_at": NOW.isoformat()}, NOW) is None
