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
