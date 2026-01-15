"""Tests for resilience patterns (retry and circuit breaker)."""

import asyncio
import time
import unittest

from drift.core.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
    RetryConfig,
    calculate_backoff_delay,
    retry_async,
)


class TestRetryConfig(unittest.TestCase):
    """Tests for RetryConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RetryConfig()
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.initial_delay_seconds, 0.1)
        self.assertEqual(config.max_delay_seconds, 10.0)
        self.assertEqual(config.exponential_base, 2.0)
        self.assertTrue(config.jitter)

    def test_custom_values(self):
        """Test custom configuration values."""
        config = RetryConfig(
            max_attempts=5,
            initial_delay_seconds=0.5,
            max_delay_seconds=30.0,
            exponential_base=3.0,
            jitter=False,
        )
        self.assertEqual(config.max_attempts, 5)
        self.assertEqual(config.initial_delay_seconds, 0.5)
        self.assertEqual(config.max_delay_seconds, 30.0)
        self.assertEqual(config.exponential_base, 3.0)
        self.assertFalse(config.jitter)


class TestCalculateBackoffDelay(unittest.TestCase):
    """Tests for backoff delay calculation."""

    def test_first_attempt_delay(self):
        """Test delay for first attempt."""
        config = RetryConfig(initial_delay_seconds=0.1, jitter=False)
        delay = calculate_backoff_delay(1, config)
        self.assertEqual(delay, 0.1)

    def test_exponential_increase(self):
        """Test exponential backoff increase."""
        config = RetryConfig(
            initial_delay_seconds=0.1,
            exponential_base=2.0,
            jitter=False,
        )
        # Attempt 1: 0.1
        # Attempt 2: 0.1 * 2^1 = 0.2
        # Attempt 3: 0.1 * 2^2 = 0.4
        self.assertEqual(calculate_backoff_delay(1, config), 0.1)
        self.assertEqual(calculate_backoff_delay(2, config), 0.2)
        self.assertEqual(calculate_backoff_delay(3, config), 0.4)

    def test_respects_max_delay(self):
        """Test that delay is capped at max_delay_seconds."""
        config = RetryConfig(
            initial_delay_seconds=1.0,
            max_delay_seconds=5.0,
            exponential_base=10.0,
            jitter=False,
        )
        # Attempt 1: 1.0
        # Attempt 2: 1.0 * 10^1 = 10.0 -> capped to 5.0
        self.assertEqual(calculate_backoff_delay(1, config), 1.0)
        self.assertEqual(calculate_backoff_delay(2, config), 5.0)

    def test_jitter_adds_randomness(self):
        """Test that jitter adds randomness to delay."""
        config = RetryConfig(
            initial_delay_seconds=1.0,
            jitter=True,
        )
        delays = [calculate_backoff_delay(1, config) for _ in range(10)]
        # With jitter, delays should vary (±25%)
        self.assertTrue(0.75 <= min(delays) < max(delays) <= 1.25)


class TestRetryAsync(unittest.TestCase):
    """Tests for async retry logic."""

    def test_successful_operation(self):
        """Test that successful operation returns immediately."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            return "success"

        result = asyncio.run(retry_async(operation))
        self.assertEqual(result, "success")
        self.assertEqual(call_count, 1)

    def test_retries_on_failure(self):
        """Test that operation is retried on failure."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Fail")
            return "success"

        config = RetryConfig(
            max_attempts=3,
            initial_delay_seconds=0.01,
        )
        result = asyncio.run(retry_async(operation, config=config, operation_name="test_op"))
        self.assertEqual(result, "success")
        self.assertEqual(call_count, 3)

    def test_raises_after_max_attempts(self):
        """Test that exception is raised after max attempts."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise ValueError("Always fails")

        config = RetryConfig(max_attempts=3, initial_delay_seconds=0.01)
        with self.assertRaises(ValueError):
            asyncio.run(retry_async(operation, config=config))
        self.assertEqual(call_count, 3)

    def test_respects_retryable_exceptions(self):
        """Test that only specified exceptions trigger retry."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise TypeError("Not retryable")

        config = RetryConfig(max_attempts=3, initial_delay_seconds=0.01)
        # Only retry ValueError, not TypeError
        with self.assertRaises(TypeError):
            asyncio.run(
                retry_async(
                    operation,
                    config=config,
                    retryable_exceptions=(ValueError,),
                )
            )
        # Should only be called once since TypeError is not retryable
        self.assertEqual(call_count, 1)


class TestCircuitBreakerConfig(unittest.TestCase):
    """Tests for CircuitBreakerConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = CircuitBreakerConfig()
        self.assertEqual(config.failure_threshold, 5)
        self.assertEqual(config.success_threshold, 2)
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertEqual(config.failure_window_seconds, 60.0)


class TestCircuitBreaker(unittest.TestCase):
    """Tests for CircuitBreaker."""

    def test_initial_state_is_closed(self):
        """Test that circuit starts in closed state."""
        cb = CircuitBreaker("test")
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.is_closed)

    def test_allows_requests_when_closed(self):
        """Test that requests are allowed when circuit is closed."""
        cb = CircuitBreaker("test")
        self.assertTrue(cb.allow_request())

    def test_opens_after_failure_threshold(self):
        """Test that circuit opens after reaching failure threshold."""
        config = CircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)

        for _ in range(3):
            cb.allow_request()
            cb.record_failure()

        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.is_closed)

    def test_rejects_requests_when_open(self):
        """Test that requests are rejected when circuit is open."""
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()

        self.assertFalse(cb.allow_request())
        self.assertEqual(cb.stats.rejected_requests, 1)

    def test_transitions_to_half_open_after_timeout(self):
        """Test that circuit transitions to half-open after timeout."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,
        )
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        time.sleep(0.15)
        # Accessing state should trigger transition
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

    def test_closes_after_success_threshold_in_half_open(self):
        """Test that circuit closes after success threshold in half-open."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            success_threshold=2,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        # Open the circuit
        cb.allow_request()
        cb.record_failure()

        # Wait for timeout to transition to half-open
        time.sleep(0.02)
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Record successes
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_reopens_on_failure_in_half_open(self):
        """Test that circuit reopens on failure in half-open state."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        # Open the circuit
        cb.allow_request()
        cb.record_failure()

        # Wait for timeout
        time.sleep(0.02)
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Record failure in half-open
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_success_clears_failures_in_closed_state(self):
        """Test that success in closed state prunes old failures."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            failure_window_seconds=0.1,
        )
        cb = CircuitBreaker("test", config)

        # Record some failures
        cb.allow_request()
        cb.record_failure()
        cb.allow_request()
        cb.record_failure()

        # Wait for failures to age out of window
        time.sleep(0.15)

        # Record success (should prune old failures)
        cb.record_success()

        # Now record more failures - shouldn't open immediately
        cb.allow_request()
        cb.record_failure()
        cb.allow_request()
        cb.record_failure()

        # Circuit should still be closed (only 2 recent failures)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_reset_closes_circuit(self):
        """Test that reset returns circuit to closed state."""
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

        cb.reset()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_stats_tracking(self):
        """Test that statistics are tracked correctly."""
        config = CircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_success()
        cb.allow_request()
        cb.record_failure()
        cb.allow_request()
        cb.record_failure()

        stats = cb.stats
        self.assertEqual(stats.total_requests, 3)
        self.assertEqual(stats.successful_requests, 1)
        self.assertEqual(stats.failed_requests, 2)


class TestCircuitOpenError(unittest.TestCase):
    """Tests for CircuitOpenError."""

    def test_error_message(self):
        """Test error message format."""
        error = CircuitOpenError("api_export")
        self.assertEqual(error.circuit_name, "api_export")
        self.assertIn("api_export", str(error))
        self.assertIn("open", str(error))


if __name__ == "__main__":
    unittest.main()
