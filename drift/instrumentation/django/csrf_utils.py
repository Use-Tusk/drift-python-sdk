"""Django CSRF token utilities for consistent record/replay testing.

This module provides utilities to normalize CSRF tokens so that recorded
and replayed responses produce identical output for comparison.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

CSRF_PLACEHOLDER = "__DRIFT_CSRF__"


def normalize_csrf_in_body(body: bytes | None) -> bytes | None:
    """Normalize CSRF tokens in response body for consistent record/replay comparison.

    Replaces Django CSRF tokens with a fixed placeholder so that recorded
    responses match replayed responses during comparison.

    This should be called after the response is sent to the browser,
    but before storing in the span. The actual response to the browser
    is unchanged.

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

        return body_str.encode("utf-8")

    except Exception as e:
        logger.debug(f"Error normalizing CSRF tokens: {e}")
        return body
