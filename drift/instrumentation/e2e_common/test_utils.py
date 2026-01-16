"""
Shared test utilities for e2e tests.

This module provides common functions used across all instrumentation e2e tests,
including request counting for validation.
"""

import time

import requests

BASE_URL = "http://localhost:8000"
_request_count = 0


def make_request(method, endpoint, **kwargs):
    """Make HTTP request, log result, and track count."""
    global _request_count
    _request_count += 1

    url = f"{BASE_URL}{endpoint}"
    print(f"→ {method} {endpoint}")
    kwargs.setdefault("timeout", 30)
    response = requests.request(method, url, **kwargs)
    print(f"  Status: {response.status_code}")
    time.sleep(0.5)
    return response


def get_request_count():
    """Return the current request count."""
    return _request_count


def print_request_summary():
    """Print the total request count in a parseable format."""
    print(f"\nTOTAL_REQUESTS_SENT:{_request_count}")
