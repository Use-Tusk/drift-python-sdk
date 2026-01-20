"""Tests for sampling.py - Sampling utilities for the Drift SDK."""

from __future__ import annotations

from drift.core.sampling import should_sample, validate_sampling_rate


class TestShouldSample:
    """Tests for should_sample function."""

    def test_always_samples_when_app_not_ready(self):
        """Should always return True when app is not ready (pre-app-start)."""
        # Even with 0% sampling rate, should sample during startup
        assert should_sample(0.0, is_app_ready=False) is True
        assert should_sample(0.5, is_app_ready=False) is True
        assert should_sample(1.0, is_app_ready=False) is True

    def test_always_samples_with_rate_1(self):
        """Should always return True when sampling rate is 1.0."""
        # Run multiple times to ensure consistency
        for _ in range(100):
            assert should_sample(1.0, is_app_ready=True) is True

    def test_never_samples_with_rate_0(self):
        """Should always return False when sampling rate is 0.0."""
        for _ in range(100):
            assert should_sample(0.0, is_app_ready=True) is False

    def test_samples_proportionally_with_rate_0_5(self):
        """Should sample approximately 50% with rate 0.5."""
        samples = [should_sample(0.5, is_app_ready=True) for _ in range(1000)]
        sample_rate = sum(samples) / len(samples)
        # Allow 10% tolerance
        assert 0.40 <= sample_rate <= 0.60, f"Sample rate {sample_rate} not within expected range"

    def test_samples_proportionally_with_rate_0_1(self):
        """Should sample approximately 10% with rate 0.1."""
        samples = [should_sample(0.1, is_app_ready=True) for _ in range(1000)]
        sample_rate = sum(samples) / len(samples)
        # Allow 5% tolerance
        assert 0.05 <= sample_rate <= 0.15, f"Sample rate {sample_rate} not within expected range"

    def test_samples_when_random_below_rate(self, mocker):
        """Should sample when random value is below rate."""
        mocker.patch("drift.core.sampling.random.random", return_value=0.3)
        assert should_sample(0.5, is_app_ready=True) is True

    def test_does_not_sample_when_random_above_rate(self, mocker):
        """Should not sample when random value is above rate."""
        mocker.patch("drift.core.sampling.random.random", return_value=0.7)
        assert should_sample(0.5, is_app_ready=True) is False

    def test_boundary_condition_random_equals_rate(self, mocker):
        """Should not sample when random equals rate (< not <=)."""
        mocker.patch("drift.core.sampling.random.random", return_value=0.5)
        assert should_sample(0.5, is_app_ready=True) is False


class TestValidateSamplingRate:
    """Tests for validate_sampling_rate function."""

    def test_valid_rate_zero(self):
        """Should accept 0.0 as valid rate."""
        assert validate_sampling_rate(0.0) == 0.0

    def test_valid_rate_one(self):
        """Should accept 1.0 as valid rate."""
        assert validate_sampling_rate(1.0) == 1.0

    def test_valid_rate_middle(self):
        """Should accept rates between 0 and 1."""
        assert validate_sampling_rate(0.5) == 0.5
        assert validate_sampling_rate(0.25) == 0.25
        assert validate_sampling_rate(0.99) == 0.99

    def test_none_returns_none(self):
        """Should return None when rate is None."""
        assert validate_sampling_rate(None) is None

    def test_negative_rate_returns_none(self):
        """Should return None and warn for negative rates."""
        assert validate_sampling_rate(-0.1) is None
        assert validate_sampling_rate(-1.0) is None

    def test_rate_above_one_returns_none(self):
        """Should return None and warn for rates above 1.0."""
        assert validate_sampling_rate(1.1) is None
        assert validate_sampling_rate(2.0) is None

    def test_custom_source_in_warning(self):
        """Should include custom source in warning message."""
        # Just verify it doesn't raise with custom source
        result = validate_sampling_rate(-0.5, source="env var TUSK_SAMPLING_RATE")
        assert result is None

    def test_converts_to_float(self):
        """Should convert integer to float."""
        result = validate_sampling_rate(1)
        assert result == 1.0
        assert isinstance(result, float)

    def test_boundary_values(self):
        """Should handle boundary values correctly."""
        # Exactly 0 and 1 should be valid
        assert validate_sampling_rate(0.0) == 0.0
        assert validate_sampling_rate(1.0) == 1.0

        # Just outside boundaries should be invalid
        assert validate_sampling_rate(-0.0001) is None
        assert validate_sampling_rate(1.0001) is None
