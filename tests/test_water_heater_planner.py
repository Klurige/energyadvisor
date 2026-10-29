"""Tests for water-heater planner primitives."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.energyadvisor.water_heater_planner import (
    DailyObservation,
    build_daily_observation,
    build_schedule_plan,
    compute_max_hours_adequacy,
    derive_thresholds,
    estimate_runtime,
)

TZ = ZoneInfo("Europe/Stockholm")


def test_threshold_detection_finds_distinct_clusters() -> None:
    samples = [5.0] * 500 + [1900.0] * 500

    thresholds = derive_thresholds(samples)

    assert thresholds.available is True
    assert thresholds.confidence == "ready"
    assert thresholds.non_heating_w is not None
    assert thresholds.active_heating_w is not None
    assert thresholds.active_heating_w > thresholds.non_heating_w


def test_threshold_detection_reports_insufficient_separation() -> None:
    samples = [100.0] * 500 + [220.0] * 500

    thresholds = derive_thresholds(samples)

    assert thresholds.available is False
    assert thresholds.confidence == "insufficient_separation"


def test_daily_observation_splits_cross_midnight_runtime() -> None:
    thresholds = derive_thresholds([0.0] * 500 + [1800.0] * 500)
    samples = [
        (datetime(2026, 1, 1, 0, 0, tzinfo=TZ), 0.0),
        (datetime(2026, 1, 1, 23, 30, tzinfo=TZ), 1800.0),
        (datetime(2026, 1, 2, 0, 30, tzinfo=TZ), 0.0),
    ]

    jan1 = build_daily_observation(
        day=datetime(2026, 1, 1, tzinfo=TZ).date(),
        tz_name="Europe/Stockholm",
        power_samples=samples,
        shower_detected=False,
        thresholds=thresholds,
    )
    jan2 = build_daily_observation(
        day=datetime(2026, 1, 2, tzinfo=TZ).date(),
        tz_name="Europe/Stockholm",
        power_samples=samples,
        shower_detected=True,
        thresholds=thresholds,
    )

    assert jan1.valid is True
    assert jan2.valid is True
    assert jan1.runtime_hours == 0.5
    assert jan2.runtime_hours == 0.5
    assert jan1.category == "no_shower"
    assert jan2.category == "shower"


def test_runtime_estimate_uses_sparse_hierarchy_and_blending() -> None:
    observations = [
        DailyObservation(
            day=datetime(2026, 1, 1).date() + timedelta(days=index),
            weekday=2,
            category="shower",
            runtime_hours=5.0,
            valid=True,
            excluded_reason=None,
            coverage_ratio=1.0,
            max_gap_minutes=5.0,
            active_segments=2,
        )
        for index in range(12)
    ]

    estimate = estimate_runtime(observations, weekday=2, category="shower")

    assert estimate.source_level == "same_weekday_same_category"
    assert estimate.sample_count == 12
    assert 4.0 < estimate.required_hours < 5.1


def test_schedule_plan_selects_cheapest_slots_and_marks_cap() -> None:
    now = datetime(2026, 3, 1, 20, 0, tzinfo=TZ)
    slots = []
    for index, cost in enumerate([2.0, 1.0, 4.0, 0.5, 3.0, 0.9]):
        start = now + timedelta(minutes=15 * index)
        slots.append(
            {"start": start, "end": start + timedelta(minutes=15), "cost": cost}
        )

    plan = build_schedule_plan(
        now_local=now,
        today_category="shower",
        shower_detected_today=True,
        required_runtime_hours=2.0,
        max_hours=1.0,
        rated_power_kw=3.0,
        raw_price_slots=slots,
        tz_name="Europe/Stockholm",
    )

    assert plan.runtime_capped is True
    assert plan.complete is False
    assert len(plan.selected_slots) == 4
    assert sorted(slot.cost for slot in plan.selected_slots) == [0.5, 0.9, 1.0, 2.0]


def test_max_hours_adequacy_needs_repeated_evidence() -> None:
    observations = []
    for index in range(12):
        observations.append(
            DailyObservation(
                day=datetime(2026, 1, 1).date() + timedelta(days=index),
                weekday=index % 7,
                category="shower",
                runtime_hours=6.0 if index < 3 else 3.0,
                valid=True,
                excluded_reason=None,
                coverage_ratio=1.0,
                max_gap_minutes=5.0,
                active_segments=2,
            )
        )

    adequacy, evidence = compute_max_hours_adequacy(observations, max_hours=4.0)

    assert adequacy == "insufficient"
    assert evidence["exceed_days"] == 3
