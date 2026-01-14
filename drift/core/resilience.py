"""Resilience patterns for reliable span export.

Provides retry with exponential backoff and circuit breaker patterns.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    initial_delay_seconds: float = 0.1
    max_delay_seconds: float = 10.0
    exponential_base: float = 2.0
    jitter: bool = True  # Prevent thundering herd


def calculate_backoff_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """Calculate delay for a given retry attempt using exponential backoff.

    Args:
        attempt: Current attempt number (1-based)
        config: Retry configuration

    Returns:
        Delay in seconds before next retry
    """
    delay = config.initial_delay_seconds * (config.exponential_base ** (attempt - 1))
    delay = min(delay, config.max_delay_seconds)

    if config.jitter:
        jitter_factor = 0.75 + random.random() * 0.5
        delay *= jitter_factor

    return delay


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    config: RetryConfig | None = None,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """Execute an async operation with retry and exponential backoff.

    Args:
        operation: Async callable to execute
        config: Retry configuration (uses defaults if None)
        retryable_exceptions: Tuple of exception types that trigger retry
        operation_name: Name for logging purposes

    Returns:
        Result of the operation

    Raises:
        The last exception if all retries are exhausted
    """
    config = config or RetryConfig()
    last_exception: Exception | None = None

    for attempt in range(1, config.max_attempts + 1):
        try:
            return await operation()
        except retryable_exceptions as e:
            last_exception = e

            if attempt == config.max_attempts:
                logger.warning(f"{operation_name} failed after {config.max_attempts} attempts: {e}")
                raise

            delay = calculate_backoff_delay(attempt, config)
            logger.debug(f"{operation_name} attempt {attempt} failed: {e}. Retrying in {delay:.2f}s...")
            await asyncio.sleep(delay)

    # Should never reach here, but satisfy type checker
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected retry loop exit")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests pass through
    OPEN = "open"  # Circuit tripped, requests fail fast
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    failure_threshold: int = 5  # Failures before opening circuit
    success_threshold: int = 2  # Successes in half-open to close circuit
    timeout_seconds: float = 30.0  # Time before transitioning open -> half-open
    # Count failures in this time window (0 = no window, count all)
    failure_window_seconds: float = 60.0


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rejected_requests: int = 0  # Requests rejected due to open circuit
    state_transitions: int = 0


class CircuitBreaker:
    """Circuit breaker for protecting against cascading failures.

    States:
    - CLOSED: Normal operation. Failures are counted.
    - OPEN: Circuit tripped after too many failures. Requests fail fast.
    - HALF_OPEN: After timeout, allow limited requests to test recovery.
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._name = name
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._lock = threading.Lock()

        # Failure tracking
        self._failure_timestamps: list[float] = []
        self._consecutive_successes = 0

        # State tracking
        self._last_failure_time: float = 0
        self._last_state_change_time: float = time.monotonic()

        # Statistics
        self._stats = CircuitBreakerStats()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state (may trigger state transition)."""
        with self._lock:
            self._check_state_transition()
            return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get circuit breaker statistics."""
        return self._stats

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (allowing requests)."""
        return self.state == CircuitState.CLOSED

    def allow_request(self) -> bool:
        """Check if a request should be allowed through.

        Returns:
            True if request should proceed, False if it should fail fast
        """
        with self._lock:
            self._check_state_transition()
            self._stats.total_requests += 1

            if self._state == CircuitState.CLOSED:
                return True
            elif self._state == CircuitState.HALF_OPEN:
                # Allow limited requests in half-open state
                return True
            else:  # OPEN
                self._stats.rejected_requests += 1
                return False

    def record_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            self._stats.successful_requests += 1

            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self._config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                self._prune_old_failures()

    def record_failure(self) -> None:
        """Record a failed request."""
        now = time.monotonic()

        with self._lock:
            self._stats.failed_requests += 1
            self._last_failure_time = now
            self._failure_timestamps.append(now)
            self._consecutive_successes = 0

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open trips the circuit again
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                self._prune_old_failures()
                if len(self._failure_timestamps) >= self._config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def _prune_old_failures(self) -> None:
        """Remove failures outside the time window."""
        if self._config.failure_window_seconds <= 0:
            return

        cutoff = time.monotonic() - self._config.failure_window_seconds
        self._failure_timestamps = [ts for ts in self._failure_timestamps if ts > cutoff]

    def _check_state_transition(self) -> None:
        """Check if state should transition based on time."""
        if self._state == CircuitState.OPEN:
            time_since_open = time.monotonic() - self._last_state_change_time
            if time_since_open >= self._config.timeout_seconds:
                self._transition_to(CircuitState.HALF_OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        from .metrics import get_metrics_collector

        old_state = self._state
        self._state = new_state
        self._last_state_change_time = time.monotonic()
        self._stats.state_transitions += 1

        if new_state == CircuitState.CLOSED:
            self._failure_timestamps.clear()
            self._consecutive_successes = 0
            # Notify metrics collector that circuit is healthy again
            get_metrics_collector().notify_circuit_breaker_closed()
        elif new_state == CircuitState.HALF_OPEN:
            self._consecutive_successes = 0
        elif new_state == CircuitState.OPEN:
            # Warn about circuit breaker opening
            get_metrics_collector().warn_circuit_breaker_open()

        logger.info(f"Circuit breaker '{self._name}' transitioned: {old_state.value} -> {new_state.value}")

    def reset(self) -> None:
        """Reset circuit breaker to initial closed state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_timestamps.clear()
            self._consecutive_successes = 0
            self._last_state_change_time = time.monotonic()
            logger.debug(f"Circuit breaker '{self._name}' reset to CLOSED")


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and request is rejected."""

    def __init__(self, circuit_name: str) -> None:
        self.circuit_name = circuit_name
        super().__init__(f"Circuit breaker '{circuit_name}' is open")
