"""Sampling utilities for the Drift SDK."""

from __future__ import annotations

import random


def should_sample(sampling_rate: float, is_app_ready: bool) -> bool:
    """
    Determine if a request should be sampled based on the sampling rate.

    Args:
        sampling_rate: Float between 0.0 and 1.0 indicating the fraction of
                      requests to sample. 1.0 means sample everything.
        is_app_ready: Whether the application has started. If False, always
                     sample to capture pre-app-start spans.

    Returns:
        True if the request should be sampled, False otherwise.
    """
    # Always sample if app is not ready (capture startup spans)
    if not is_app_ready:
        return True

    # Random sampling based on rate
    return random.random() < sampling_rate


def validate_sampling_rate(rate: float | None, source: str = "config") -> float | None:
    """
    Validate a sampling rate value.

    Args:
        rate: The sampling rate to validate
        source: Description of where the rate came from (for error messages)

    Returns:
        The validated rate, or None if invalid
    """
    if rate is None:
        return None

    if not isinstance(rate, (int, float)):
        print(f"Warning: Invalid sampling rate from {source}: not a number. Ignoring.")
        return None

    if rate < 0.0 or rate > 1.0:
        print(f"Warning: Invalid sampling rate from {source}: {rate}. Must be between 0.0 and 1.0. Ignoring.")
        return None

    return float(rate)
