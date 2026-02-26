"""Shared external HTTP config/helpers for instrumentation e2e apps."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULT_MOCK_BASE_URL = "http://mock-upstream:8081"
_DEFAULT_TIMEOUT_SECONDS = 3.0


def use_mock_externals() -> bool:
    return os.getenv("USE_MOCK_EXTERNALS", "0").strip().lower() in _TRUTHY


def mock_server_base_url() -> str:
    return os.getenv("MOCK_SERVER_BASE_URL", _DEFAULT_MOCK_BASE_URL).rstrip("/")


def external_http_timeout_seconds() -> float:
    raw = os.getenv("EXTERNAL_HTTP_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


def upstream_url(url: str) -> str:
    """Rewrite external URLs to the local mock upstream when enabled."""
    if not use_mock_externals():
        return url

    source = urlsplit(url)
    if not source.scheme or not source.netloc:
        return url

    target = urlsplit(mock_server_base_url())
    path = source.path or "/"
    return urlunsplit((target.scheme or "http", target.netloc, path, source.query, ""))


def upstream_url_parts(url: str) -> tuple[str, str, int, str]:
    """Return (scheme, host, port, path_with_query) after mock rewrite."""
    rewritten = upstream_url(url)
    parsed = urlsplit(rewritten)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, host, port, path
