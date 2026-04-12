"""Adaptive sampling controller for inbound root-request admission."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

SamplingMode = Literal["fixed", "adaptive"]
AdaptiveSamplingState = Literal["fixed", "healthy", "warm", "hot", "critical_pause"]
RootSamplingDecisionReason = Literal[
    "pre_app_start",
    "sampled",
    "not_sampled",
    "load_shed",
    "critical_pause",
]


@dataclass
class ResolvedSamplingConfig:
    mode: SamplingMode
    base_rate: float
    min_rate: float


@dataclass
class AdaptiveSamplingHealthSnapshot:
    queue_fill_ratio: float | None = None
    dropped_span_count: int = 0
    export_failure_count: int = 0
    export_circuit_open: bool = False
    memory_pressure_ratio: float | None = None


@dataclass
class RootSamplingDecision:
    should_record: bool
    reason: RootSamplingDecisionReason
    mode: SamplingMode
    state: AdaptiveSamplingState
    base_rate: float
    min_rate: float
    effective_rate: float
    admission_multiplier: float


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return min(max_value, max(min_value, value))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _normalize_between(value: float | None, zero_point: float, one_point: float) -> float:
    if value is None or one_point <= zero_point:
        return 0.0
    return _clamp01((value - zero_point) / (one_point - zero_point))


class AdaptiveSamplingController:
    def __init__(
        self,
        config: ResolvedSamplingConfig,
        *,
        random_fn=random.random,
        now_fn=time.monotonic,
    ) -> None:
        self._config = config
        self._random_fn = random_fn
        self._now_fn = now_fn
        self._lock = threading.RLock()

        self._admission_multiplier = 1.0
        self._state: AdaptiveSamplingState = "fixed" if config.mode == "fixed" else "healthy"
        self._paused_until_s = 0.0
        self._last_updated_at_s: float | None = None
        self._last_decrease_at_s = 0.0

        self._prev_dropped_span_count = 0
        self._prev_export_failure_count = 0

        self._queue_fill_ewma: float | None = None
        self._recent_drop_signal = 0.0
        self._recent_failure_signal = 0.0

    def update(self, snapshot: AdaptiveSamplingHealthSnapshot) -> None:
        with self._lock:
            if self._config.mode != "adaptive":
                self._state = "fixed"
                self._admission_multiplier = 1.0
                return

            now_s = self._now_fn()
            elapsed_s = 2.0 if self._last_updated_at_s is None else max(0.001, now_s - self._last_updated_at_s)
            self._last_updated_at_s = now_s

            decay = math.exp(-(elapsed_s * 1000.0) / 30000.0)
            self._recent_drop_signal *= decay
            self._recent_failure_signal *= decay

            dropped_delta = max(0, snapshot.dropped_span_count - self._prev_dropped_span_count)
            export_failure_delta = max(0, snapshot.export_failure_count - self._prev_export_failure_count)

            self._prev_dropped_span_count = snapshot.dropped_span_count
            self._prev_export_failure_count = snapshot.export_failure_count

            self._recent_drop_signal += dropped_delta
            self._recent_failure_signal += export_failure_delta

            if snapshot.queue_fill_ratio is not None:
                queue_fill_ratio = _clamp01(snapshot.queue_fill_ratio)
                self._queue_fill_ewma = (
                    queue_fill_ratio
                    if self._queue_fill_ewma is None
                    else (0.25 * queue_fill_ratio) + (0.75 * self._queue_fill_ewma)
                )

            queue_pressure = _normalize_between(self._queue_fill_ewma, 0.20, 0.85)
            memory_pressure = _normalize_between(snapshot.memory_pressure_ratio, 0.80, 0.92)
            export_failure_pressure = _clamp01(self._recent_failure_signal / 5.0)
            pressure = max(queue_pressure, memory_pressure, export_failure_pressure)

            hard_brake = (
                dropped_delta > 0 or snapshot.export_circuit_open or (snapshot.memory_pressure_ratio or 0.0) >= 0.92
            )

            previous_state = self._state
            previous_multiplier = self._admission_multiplier

            if hard_brake:
                self._paused_until_s = now_s + 15.0
                self._admission_multiplier = 0.0
                self._state = "critical_pause"
                self._last_decrease_at_s = now_s
                self._log_transition(previous_state, previous_multiplier, pressure, snapshot)
                return

            if now_s < self._paused_until_s:
                self._state = "critical_pause"
                self._log_transition(previous_state, previous_multiplier, pressure, snapshot)
                return

            min_multiplier = self._get_min_multiplier()
            if pressure >= 0.70:
                self._admission_multiplier = max(min_multiplier, self._admission_multiplier * 0.4)
                self._state = "hot"
                self._last_decrease_at_s = now_s
            elif pressure >= 0.45:
                self._admission_multiplier = max(min_multiplier, self._admission_multiplier * 0.7)
                self._state = "warm"
                self._last_decrease_at_s = now_s
            else:
                if pressure <= 0.20 and (now_s - self._last_decrease_at_s) >= 10.0:
                    self._admission_multiplier = min(1.0, self._admission_multiplier + 0.05)
                self._state = "healthy"

            self._log_transition(previous_state, previous_multiplier, pressure, snapshot)

    def get_decision(self, *, is_pre_app_start: bool) -> RootSamplingDecision:
        with self._lock:
            if is_pre_app_start:
                return RootSamplingDecision(
                    should_record=True,
                    reason="pre_app_start",
                    mode=self._config.mode,
                    state=self._state,
                    base_rate=self._config.base_rate,
                    min_rate=self._config.min_rate,
                    effective_rate=1.0,
                    admission_multiplier=1.0,
                )

            effective_rate = (
                self.get_effective_sampling_rate()
                if self._config.mode == "adaptive"
                else _clamp01(self._config.base_rate)
            )

            if effective_rate <= 0.0:
                return RootSamplingDecision(
                    should_record=False,
                    reason="critical_pause" if self._state == "critical_pause" else "not_sampled",
                    mode=self._config.mode,
                    state=self._state,
                    base_rate=self._config.base_rate,
                    min_rate=self._config.min_rate,
                    effective_rate=effective_rate,
                    admission_multiplier=self._admission_multiplier,
                )

            should_record = self._random_fn() < effective_rate
            return RootSamplingDecision(
                should_record=should_record,
                reason=(
                    "sampled"
                    if should_record
                    else "load_shed"
                    if self._config.mode == "adaptive" and effective_rate < self._config.base_rate
                    else "not_sampled"
                ),
                mode=self._config.mode,
                state=self._state,
                base_rate=self._config.base_rate,
                min_rate=self._config.min_rate,
                effective_rate=effective_rate,
                admission_multiplier=self._admission_multiplier if self._config.mode == "adaptive" else 1.0,
            )

    def get_effective_sampling_rate(self) -> float:
        with self._lock:
            if self._config.mode != "adaptive":
                return _clamp01(self._config.base_rate)
            if self._state == "critical_pause" and self._now_fn() < self._paused_until_s:
                return 0.0
            effective_rate = self._config.base_rate * self._admission_multiplier
            return _clamp(
                effective_rate,
                min(self._config.base_rate, self._config.min_rate),
                self._config.base_rate,
            )

    def _get_min_multiplier(self) -> float:
        if self._config.base_rate <= 0.0 or self._config.min_rate <= 0.0:
            return 0.0
        return _clamp01(self._config.min_rate / self._config.base_rate)

    def _log_transition(
        self,
        previous_state: AdaptiveSamplingState,
        previous_multiplier: float,
        pressure: float,
        snapshot: AdaptiveSamplingHealthSnapshot,
    ) -> None:
        if previous_state == self._state and abs(previous_multiplier - self._admission_multiplier) < 0.05:
            return

        logger.info(
            "Adaptive sampling updated (state=%s, multiplier=%.2f, effective_rate=%.4f, pressure=%.2f, queue_fill=%s, memory_pressure_ratio=%s, export_circuit_open=%s).",
            self._state,
            self._admission_multiplier,
            self.get_effective_sampling_rate(),
            pressure,
            f"{self._queue_fill_ewma:.2f}" if self._queue_fill_ewma is not None else "n/a",
            snapshot.memory_pressure_ratio if snapshot.memory_pressure_ratio is not None else "n/a",
            snapshot.export_circuit_open,
        )
