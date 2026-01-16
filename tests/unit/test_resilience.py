"""Tests for resilience patterns (retry and circuit breaker)."""

import asyncio
import time

import pytest

from drift.core.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
    RetryConfig,
    calculate_backoff_delay,
    retry_async,
)


class TestRetryConfig:
    """Tests for RetryConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.initial_delay_seconds == 0.1
        assert config.max_delay_seconds == 10.0
        assert config.exponential_base == 2.0
        assert config.jitter is True

    def test_custom_values(self):
        """Test custom configuration values."""
        config = RetryConfig(
            max_attempts=5,
            initial_delay_seconds=0.5,
            max_delay_seconds=30.0,
            exponential_base=3.0,
            jitter=False,
        )
        assert config.max_attempts == 5
        assert config.initial_delay_seconds == 0.5
        assert config.max_delay_seconds == 30.0
        assert config.exponential_base == 3.0
        assert config.jitter is False


class TestCalculateBackoffDelay:
    """Tests for backoff delay calculation."""

    def test_first_attempt_delay(self):
        """Test delay for first attempt."""
        config = RetryConfig(initial_delay_seconds=0.1, jitter=False)
        delay = calculate_backoff_delay(1, config)
        assert delay == 0.1

    def test_exponential_increase(self):
        """Test exponential backoff increase."""
        config = RetryConfig(
            initial_delay_seconds=0.1,
            exponential_base=2.0,
            jitter=False,
        )
        assert calculate_backoff_delay(1, config) == 0.1
        assert calculate_backoff_delay(2, config) == 0.2
        assert calculate_backoff_delay(3, config) == 0.4

    def test_respects_max_delay(self):
        """Test that delay is capped at max_delay_seconds."""
        config = RetryConfig(
            initial_delay_seconds=1.0,
            max_delay_seconds=5.0,
            exponential_base=10.0,
            jitter=False,
        )
        assert calculate_backoff_delay(1, config) == 1.0
        assert calculate_backoff_delay(2, config) == 5.0

    def test_jitter_adds_randomness(self):
        """Test that jitter adds randomness to delay."""
        config = RetryConfig(
            initial_delay_seconds=1.0,
            jitter=True,
        )
        delays = [calculate_backoff_delay(1, config) for _ in range(10)]
        assert 0.75 <= min(delays) < max(delays) <= 1.25


class TestRetryAsync:
    """Tests for async retry logic."""

    def test_successful_operation(self):
        """Test that successful operation returns immediately."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            return "success"

        result = asyncio.run(retry_async(operation))
        assert result == "success"
        assert call_count == 1

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
        assert result == "success"
        assert call_count == 3

    def test_raises_after_max_attempts(self):
        """Test that exception is raised after max attempts."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise ValueError("Always fails")

        config = RetryConfig(max_attempts=3, initial_delay_seconds=0.01)
        with pytest.raises(ValueError):
            asyncio.run(retry_async(operation, config=config))
        assert call_count == 3

    def test_respects_retryable_exceptions(self):
        """Test that only specified exceptions trigger retry."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise TypeError("Not retryable")

        config = RetryConfig(max_attempts=3, initial_delay_seconds=0.01)
        with pytest.raises(TypeError):
            asyncio.run(
                retry_async(
                    operation,
                    config=config,
                    retryable_exceptions=(ValueError,),
                )
            )
        assert call_count == 1


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = CircuitBreakerConfig()
        assert config.failure_threshold == 5
        assert config.success_threshold == 2
        assert config.timeout_seconds == 30.0
        assert config.failure_window_seconds == 60.0


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    def test_initial_state_is_closed(self):
        """Test that circuit starts in closed state."""
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed is True

    def test_allows_requests_when_closed(self):
        """Test that requests are allowed when circuit is closed."""
        cb = CircuitBreaker("test")
        assert cb.allow_request() is True

    def test_opens_after_failure_threshold(self):
        """Test that circuit opens after reaching failure threshold."""
        config = CircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)

        for _ in range(3):
            cb.allow_request()
            cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.is_closed is False

    def test_rejects_requests_when_open(self):
        """Test that requests are rejected when circuit is open."""
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()

        assert cb.allow_request() is False
        assert cb.stats.rejected_requests == 1

    def test_transitions_to_half_open_after_timeout(self):
        """Test that circuit transitions to half-open after timeout."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,
        )
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_closes_after_success_threshold_in_half_open(self):
        """Test that circuit closes after success threshold in half-open."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            success_threshold=2,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()

        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        """Test that circuit reopens on failure in half-open state."""
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()

        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_clears_failures_in_closed_state(self):
        """Test that success in closed state prunes old failures."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            failure_window_seconds=0.1,
        )
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()
        cb.allow_request()
        cb.record_failure()

        time.sleep(0.15)

        cb.record_success()

        cb.allow_request()
        cb.record_failure()
        cb.allow_request()
        cb.record_failure()

        assert cb.state == CircuitState.CLOSED

    def test_reset_closes_circuit(self):
        """Test that reset returns circuit to closed state."""
        config = CircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.allow_request()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED

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
        assert stats.total_requests == 3
        assert stats.successful_requests == 1
        assert stats.failed_requests == 2


class TestCircuitOpenError:
    """Tests for CircuitOpenError."""

    def test_error_message(self):
        """Test error message format."""
        error = CircuitOpenError("api_export")
        assert error.circuit_name == "api_export"
        assert "api_export" in str(error)
        assert "open" in str(error)
