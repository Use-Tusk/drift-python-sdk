import math
import threading

from drift.core.adaptive_sampling import (
    AdaptiveSamplingController,
    AdaptiveSamplingHealthSnapshot,
    ResolvedSamplingConfig,
)


def test_pre_app_start_always_records():
    controller = AdaptiveSamplingController(
        ResolvedSamplingConfig(mode="adaptive", base_rate=0.0, min_rate=0.0),
        random_fn=lambda: 0.99,
        now_fn=lambda: 0.0,
    )

    decision = controller.get_decision(is_pre_app_start=True)

    assert decision.should_record is True
    assert decision.reason == "pre_app_start"
    assert decision.effective_rate == 1.0


def test_controller_load_sheds_and_pauses_on_drops():
    now = {"value": 0.0}
    controller = AdaptiveSamplingController(
        ResolvedSamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
        random_fn=lambda: 0.3,
        now_fn=lambda: now["value"],
    )

    controller.update(AdaptiveSamplingHealthSnapshot(queue_fill_ratio=0.9))
    load_shed_decision = controller.get_decision(is_pre_app_start=False)
    assert load_shed_decision.state == "hot"
    assert load_shed_decision.effective_rate < 0.5
    assert load_shed_decision.should_record is False
    assert load_shed_decision.reason == "load_shed"

    now["value"] = 1.0
    controller.update(AdaptiveSamplingHealthSnapshot(queue_fill_ratio=0.1, dropped_span_count=1))
    paused_decision = controller.get_decision(is_pre_app_start=False)
    assert paused_decision.state == "critical_pause"
    assert paused_decision.should_record is False
    assert paused_decision.reason == "critical_pause"


def test_elapsed_time_uses_zero_timestamp_as_real_value():
    now = {"value": 0.0}
    controller = AdaptiveSamplingController(
        ResolvedSamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
        random_fn=lambda: 0.0,
        now_fn=lambda: now["value"],
    )

    controller.update(AdaptiveSamplingHealthSnapshot(export_failure_count=1))
    now["value"] = 0.5
    controller.update(AdaptiveSamplingHealthSnapshot(export_failure_count=1))

    expected_decay = math.exp(-(0.5 * 1000.0) / 30000.0)
    assert math.isclose(controller._recent_failure_signal, expected_decay, rel_tol=1e-6)


def test_get_decision_waits_for_controller_lock():
    controller = AdaptiveSamplingController(
        ResolvedSamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
        random_fn=lambda: 0.0,
        now_fn=lambda: 0.0,
    )
    started = threading.Event()
    finished = threading.Event()
    result = {}

    def worker() -> None:
        started.set()
        result["decision"] = controller.get_decision(is_pre_app_start=False)
        finished.set()

    thread = threading.Thread(target=worker)
    controller._lock.acquire()
    try:
        thread.start()
        assert started.wait(timeout=1.0)
        assert not finished.wait(timeout=0.05)
    finally:
        controller._lock.release()

    assert finished.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert result["decision"].effective_rate == 0.5


def test_update_waits_for_controller_lock():
    controller = AdaptiveSamplingController(
        ResolvedSamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
        random_fn=lambda: 0.0,
        now_fn=lambda: 0.0,
    )
    started = threading.Event()
    finished = threading.Event()

    def worker() -> None:
        started.set()
        controller.update(AdaptiveSamplingHealthSnapshot(queue_fill_ratio=0.9))
        finished.set()

    thread = threading.Thread(target=worker)
    controller._lock.acquire()
    try:
        thread.start()
        assert started.wait(timeout=1.0)
        assert not finished.wait(timeout=0.05)
    finally:
        controller._lock.release()

    assert finished.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert controller.get_decision(is_pre_app_start=False).state == "hot"
