"""Django HTML utilities for consistent record/replay testing.

This module provides utilities to normalize HTML content so that recorded
and replayed responses produce identical output for comparison. Includes
CSRF token normalization and HTML class ordering normalization.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.http import HttpResponse

logger = logging.getLogger(__name__)

# Placeholder used to replace CSRF tokens for consistent comparison
CSRF_PLACEHOLDER = "__DRIFT_CSRF__"


def normalize_csrf_in_body(body: bytes | None) -> bytes | None:
    """Normalize CSRF tokens in response body for consistent record/replay comparison.

    Replaces Django CSRF tokens with a fixed placeholder so that recorded
    responses match replayed responses during comparison.

    Args:
        body: Response body bytes (typically HTML)

    Returns:
        Body with CSRF tokens normalized, or original body if not applicable
    """
    if not body:
        return body

    try:
        body_str = body.decode("utf-8")

        # Pattern 1: Hidden input fields with csrfmiddlewaretoken
        # <input type="hidden" name="csrfmiddlewaretoken" value="ABC123...">
        # Handles both single and double quotes, various attribute orders
        csrf_input_pattern = (
            r'(<input[^>]*name=["\']csrfmiddlewaretoken["\'][^>]*value=["\'])'
            r'[^"\']+(["\'])'
        )
        body_str = re.sub(
            csrf_input_pattern,
            rf"\g<1>{CSRF_PLACEHOLDER}\2",
            body_str,
            flags=re.IGNORECASE,
        )

        # Pattern 2: Also handle value before name (different attribute order)
        # <input type="hidden" value="ABC123" name="csrfmiddlewaretoken">
        csrf_input_pattern_alt = r'(<input[^>]*value=["\'])[^"\']+(["\'][^>]*name=["\']csrfmiddlewaretoken["\'])'
        body_str = re.sub(
            csrf_input_pattern_alt,
            rf"\g<1>{CSRF_PLACEHOLDER}\2",
            body_str,
            flags=re.IGNORECASE,
        )

        # Pattern 3: JavaScript CSRF token assignment (e.g., DRF Swagger UI)
        # request.headers["X-CSRFTOKEN"] = "ABC123...";
        # Also handles single quotes and X-CSRFToken variants
        csrf_js_pattern = r'(headers\[["\']X-CSRFTOKEN["\']\]\s*=\s*["\'])[^"\']+(["\'])'
        body_str = re.sub(
            csrf_js_pattern,
            rf"\g<1>{CSRF_PLACEHOLDER}\2",
            body_str,
            flags=re.IGNORECASE,
        )

        return body_str.encode("utf-8")

    except Exception as e:
        logger.debug(f"Error normalizing CSRF tokens: {e}")
        return body


def _sort_classes(match: re.Match) -> str:
    """Sort CSS classes within a class attribute match.

    Args:
        match: Regex match object containing the class attribute

    Returns:
        The class attribute with sorted class names
    """
    prefix = match.group(1)  # 'class="' or "class='"
    classes = match.group(2)  # The actual class names
    quote = match.group(3)  # Closing quote

    # Split classes by whitespace, sort them, and rejoin
    class_list = classes.split()
    sorted_classes = " ".join(sorted(class_list))

    return f"{prefix}{sorted_classes}{quote}"


def normalize_html_class_ordering(body: bytes | None) -> bytes | None:
    """Normalize HTML class attribute ordering for consistent record/replay comparison.

    Sorts CSS class names alphabetically within each class attribute so that
    non-deterministic class ordering (e.g., from django-unfold) doesn't cause
    false deviations during replay comparison.

    Args:
        body: Response body bytes (typically HTML)

    Returns:
        Body with class attributes normalized, or original body if not applicable
    """
    if not body:
        return body

    try:
        body_str = body.decode("utf-8")

        # Pattern to match class attributes with double or single quotes
        # class="class1 class2 class3" or class='class1 class2 class3'
        # Captures: (1) prefix including quote, (2) class names, (3) closing quote
        class_pattern = r'(class=["\'])([^"\']+)(["\'])'

        body_str = re.sub(
            class_pattern,
            _sort_classes,
            body_str,
            flags=re.IGNORECASE,
        )

        return body_str.encode("utf-8")

    except Exception as e:
        logger.debug(f"Error normalizing HTML class ordering: {e}")
        return body


def normalize_html_body(body: bytes | None, content_type: str, content_encoding: str = "") -> bytes | None:
    """Normalize HTML body for consistent record/replay comparison.

    Applies CSRF token normalization and HTML class ordering normalization.
    Only processes uncompressed text/html responses.

    Args:
        body: Response body bytes
        content_type: Content-Type header value
        content_encoding: Content-Encoding header value (optional)

    Returns:
        Normalized body bytes, or original body if not applicable
    """
    if not body:
        return body

    if "text/html" not in content_type.lower():
        return body

    # Skip normalization for compressed responses - decoding gzip/deflate as UTF-8 would corrupt the body
    encoding = content_encoding.lower() if content_encoding else ""
    if encoding and encoding != "identity":
        return body

    normalized = normalize_csrf_in_body(body)
    normalized = normalize_html_class_ordering(normalized)

    return normalized


def normalize_html_response(response: HttpResponse) -> HttpResponse:
    """Normalize CSRF tokens and HTML class ordering in Django response.

    Modifies the response body in-place. Only affects uncompressed HTML responses.
    Used in REPLAY mode to ensure the actual response matches the recorded
    (normalized) response.

    Args:
        response: Django HttpResponse object

    Returns:
        Modified response with normalized body
    """
    content_type = response.get("Content-Type", "")
    content_encoding = response.get("Content-Encoding", "")

    if not hasattr(response, "content") or not response.content:
        return response

    normalized_body = normalize_html_body(response.content, content_type, content_encoding)

    if normalized_body is not None and normalized_body != response.content:
        response.content = normalized_body
        # Update Content-Length header if present
        if "Content-Length" in response:
            response["Content-Length"] = len(normalized_body)

    return response
