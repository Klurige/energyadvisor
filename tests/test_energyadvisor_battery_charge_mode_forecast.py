"""Tests for the battery charge mode forecast helpers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.energyadvisor.sensor.battery_charge_mode_forecast import (
    _compute_floor_kwh,
    _find_solar_window,
    _is_solar_dominant,
    _last_float_state_at_or_before,
    _solar_kw_for_slot,
    _solar_window_by_date,
)

UTC = timezone.utc


def _make_solar_entries(start: datetime, powers: list[float]) -> list[dict]:
    return [
        {
            "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(
                timespec="minutes"
            ),
            "pow": power,
        }
        for index, power in enumerate(powers)
    ]


def test_solar_window_by_date_groups_entries_and_skips_invalid_rows():
    entries = [
        {"end": "2024-06-01T06:15:00+00:00", "pow": 0.10},
        {"end": "2024-06-01T06:30:00+00:00", "pow": 0.20},
        {"end": "2024-06-01T07:15:00+00:00", "pow": 0.04},
        {"end": "2024-06-02T07:15:00+00:00", "pow": 0.15},
        {"end": "bad", "pow": 1.0},
        {"pow": 1.0},
    ]

    windows = _solar_window_by_date(entries, tz_hint=UTC)

    assert windows[datetime(2024, 6, 1, tzinfo=UTC).date()] == (
        datetime(2024, 6, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 6, 1, 6, 30, tzinfo=UTC),
    )
    assert windows[datetime(2024, 6, 2, tzinfo=UTC).date()] == (
        datetime(2024, 6, 2, 7, 0, tzinfo=UTC),
        datetime(2024, 6, 2, 7, 15, tzinfo=UTC),
    )


def test_is_solar_dominant_uses_daily_threshold():
    assert not _is_solar_dominant(
        _make_solar_entries(
            datetime(2024, 6, 1, 12, 0, tzinfo=UTC), [0.5, 0.5, 0.5, 0.5]
        )
    )
    assert _is_solar_dominant(
        _make_solar_entries(
            datetime(2024, 6, 1, 12, 0, tzinfo=UTC), [1.0, 1.0, 1.0, 1.0]
        )
    )


def test_solar_kw_for_slot_matches_slot_and_ignores_mismatches():
    slot_start = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    solar_entries = [
        {"end": "2024-06-01T12:15:00+00:00", "pow": 1.5},
        {"end": "2024-06-01T12:30:00+00:00", "pow": -2.0},
    ]

    assert (
        _solar_kw_for_slot(
            solar_entries, slot_start, slot_start + timedelta(minutes=15)
        )
        == 1.5
    )
    assert (
        _solar_kw_for_slot(
            solar_entries,
            slot_start + timedelta(minutes=15),
            slot_start + timedelta(minutes=30),
        )
        == 0.0
    )


def test_find_solar_window_tracks_today_and_tomorrow():
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    solar_entries = _make_solar_entries(
        datetime(2024, 6, 1, 6, 0, tzinfo=UTC), [0.2, 0.2]
    ) + _make_solar_entries(datetime(2024, 6, 2, 7, 0, tzinfo=UTC), [0.3])

    assert _find_solar_window(solar_entries, now) == (
        datetime(2024, 6, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 6, 1, 6, 30, tzinfo=UTC),
        datetime(2024, 6, 2, 7, 0, tzinfo=UTC),
    )


def test_compute_floor_kwh_nighttime_before_sunrise():
    solar_entries = [
        {"end": "2024-06-01T06:15:00+00:00", "pow": 1.0},
        {"end": "2024-06-02T06:15:00+00:00", "pow": 1.0},
    ]

    floor_kwh, required_load_kwh = _compute_floor_kwh(
        solar_entries,
        datetime(2024, 6, 1, 3, 0, tzinfo=UTC),
        base_load_kw=1.0,
        battery_capacity_kwh=10.0,
        reserve_fraction=0.1,
    )

    assert floor_kwh == pytest.approx(4.0)
    assert required_load_kwh == pytest.approx(3.0)


def test_compute_floor_kwh_daytime_uses_default_overnight_without_tomorrow():
    solar_entries = _make_solar_entries(
        datetime(2024, 6, 1, 12, 0, tzinfo=UTC), [1.0] * 24
    )

    floor_kwh, required_load_kwh = _compute_floor_kwh(
        solar_entries,
        datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        base_load_kw=1.0,
        battery_capacity_kwh=10.0,
        reserve_fraction=0.1,
    )

    assert floor_kwh == pytest.approx(8.0)
    assert required_load_kwh == pytest.approx(7.0)


def test_last_float_state_at_or_before_parses_decimal_comma_and_stops_at_cutoff():
    states = [
        SimpleNamespace(
            last_updated=datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
            state="1,25",
        ),
        SimpleNamespace(
            last_updated=datetime(2024, 6, 1, 11, 0, tzinfo=UTC),
            state="bad",
        ),
    ]

    assert _last_float_state_at_or_before(
        states, datetime(2024, 6, 1, 10, 30, tzinfo=UTC)
    ) == pytest.approx(1.25)
    assert (
        _last_float_state_at_or_before(states, datetime(2024, 6, 1, 11, 30, tzinfo=UTC))
        is None
    )
