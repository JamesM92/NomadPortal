"""Shared test setup."""

import pytest

from nomadnet_web import rate_limit


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """The rate limiter keeps its counts in a module-level store, so without
    this a test that sends several requests can be refused (429) because of
    requests made by tests that ran before it."""
    with rate_limit._lock:
        rate_limit._windows.clear()
    yield
