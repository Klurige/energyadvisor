"""Tests for BatteryChargeModeSensor and the charge-mode algorithm."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.electricitypricelevels.sensor.batterychargemodesensor import (
    _DEFAULT_CHARGING_TIME_MINUTES as CHARGING_TIME_MINUTES,
    _DEFAULT_DISCHARGING_TIME_MINUTES as DISCHARGING_TIME_MINUTES,
    _DEFAULT_MARGIN as MARGIN,
    BatteryChargeModeSensor,
    _extend_peaks,
    _find_local_peaks,
    _find_local_valleys,
    compute_charge_modes,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_rates(costs, start=None, slot_hours=1):
    """Return a list of compact rate dicts (as exposed in attributes)."""
    if start is None:
        start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    rates = []
    for i, cost in enumerate(costs):
        s = start + timedelta(hours=i * slot_hours)
        rates.append({"from": s.strftime("%Y-%m-%dT%H:%M"), "cost": cost})
    return rates


def make_charge_entries(costs, modes_override=None, start=None):
    """Return charge-entry dicts (internal datetime format) for algorithm tests."""
    if start is None:
        start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    entries = []
    for i, cost in enumerate(costs):
        s = start + timedelta(hours=i)
        e = s + timedelta(hours=1)
        entries.append({"start": s, "end": e, "mode": "standby", "cost": cost})
    if modes_override:
        for i, m in enumerate(modes_override):
            if m is not None:
                entries[i]["mode"] = m
    return entries


def make_timed_entries(modes_list, start=None):
    """Return charge entries with mode already set (cost=1.0 throughout)."""
    if start is None:
        start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    return [
        {
            "start": start + timedelta(hours=i),
            "end": start + timedelta(hours=i + 1),
            "mode": m,
            "cost": 1.0,
        }
        for i, m in enumerate(modes_list)
    ]


# ---------------------------------------------------------------------------
# _find_local_peaks
# ---------------------------------------------------------------------------


class TestFindLocalPeaks:

    def test_empty_range_no_changes(self):
        entries = make_charge_entries([1.0, 2.0, 3.0])
        _find_local_peaks(
            entries,
            datetime(2024, 1, 2, 0, 0, tzinfo=UTC),  # range outside data
            datetime(2024, 1, 3, 0, 0, tzinfo=UTC),
            0.7,
            240,
        )
        assert all(e["mode"] == "standby" for e in entries)

    def test_margin_not_met_no_changes(self):
        # peak=2.0, valley=1.9 → gap 0.1 < margin 0.7
        entries = make_charge_entries([1.9, 2.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(entries, range_start, range_end, 0.7, 240)
        assert all(e["mode"] == "standby" for e in entries)

    def test_valley_after_peak_no_changes(self):
        # Hour 0 is most expensive; all cheap hours come AFTER it → wrong order
        entries = make_charge_entries([4.0, 1.0, 1.0, 1.0, 1.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(entries, range_start, range_end, 0.7, 240)
        assert all(e["mode"] == "standby" for e in entries)

    def test_too_few_peaks_no_changes(self):
        # With only 2 entries and one candidate, peaks list stays at ≤2 → no discharge
        entries = make_charge_entries([1.0, 4.0, 4.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(entries, range_start, range_end, 0.7, 240)
        assert all(e["mode"] == "standby" for e in entries)

    def test_valid_peak_sets_discharge(self):
        # hours 0-2: cheap (1.0), hours 3-7: expensive (4.0)
        # peak=3:00, valley=2:00; 4.0-1.0=3.0 > 0.7; total_slots=4 → hours 3-7 discharged
        entries = make_charge_entries([1.0, 1.0, 1.0, 4.0, 4.0, 4.0, 4.0, 4.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(entries, range_start, range_end, 0.7, 240)
        for i, e in enumerate(entries):
            expected = "discharge" if 3 <= i <= 7 else "standby"
            assert (
                e["mode"] == expected
            ), f"Hour {i}: expected {expected}, got {e['mode']}"

    def test_discharge_limited_by_total_slots(self):
        # total_slots = round(60/60) = 1; only 1 candidate added beyond initial peak
        entries = make_charge_entries([1.0, 4.0, 4.0, 4.0, 4.0, 4.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(
            entries, range_start, range_end, 0.7, 60
        )  # 60 min → 1 extra slot
        discharge_hours = [i for i, e in enumerate(entries) if e["mode"] == "discharge"]
        # Initial peak (hour 1) + 1 extra → 2 peaks, but 2 ≤ 2 so → no discharge
        assert len(discharge_hours) == 0

    def test_discharge_with_enough_slots(self):
        # total_slots = round(180/60) = 3; initial peak + 3 extra = 4 peaks > 2 → discharge
        entries = make_charge_entries([1.0, 4.0, 4.0, 4.0, 4.0, 4.0])
        range_start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        range_end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        _find_local_peaks(entries, range_start, range_end, 0.7, 180)  # 3 extra slots
        discharge_hours = [i for i, e in enumerate(entries) if e["mode"] == "discharge"]
        assert len(discharge_hours) == 4  # hours 1, 2, 3, 4


# ---------------------------------------------------------------------------
# _find_local_valleys
# ---------------------------------------------------------------------------


class TestFindLocalValleys:

    def test_no_discharge_entries_no_changes(self):
        entries = make_timed_entries(["standby"] * 6)
        _find_local_valleys(entries, 0.7, 160)
        assert all(e["mode"] == "standby" for e in entries)

    def test_charge_set_for_cheapest_slots_before_discharge(self):
        # hours 0-2: 1.0 (cheap), hours 3-5: 4.0 (discharge)
        entries = make_charge_entries(
            [1.0, 1.0, 1.0, 4.0, 4.0, 4.0],
            modes_override=[None, None, None, "discharge", "discharge", "discharge"],
        )
        _find_local_valleys(entries, 0.7, 120)  # 120 min → 2 slots
        charge_hours = [i for i, e in enumerate(entries) if e["mode"] == "charge"]
        assert len(charge_hours) == 2
        assert 0 in charge_hours and 1 in charge_hours  # Two cheapest

    def test_lookback_capped_at_8_hours(self):
        # 10 cheap standby hours then 3 discharge hours
        # 8h cap: gap_start = 10:00 - 8h = 2:00, so hours 0,1 should NOT be charged
        entries = make_charge_entries(
            [1.0] * 10 + [4.0] * 3,
            modes_override=[None] * 10 + ["discharge", "discharge", "discharge"],
        )
        _find_local_valleys(entries, 0.7, 120)  # 2 charge slots
        charge_hours = [i for i, e in enumerate(entries) if e["mode"] == "charge"]
        assert len(charge_hours) == 2
        assert all(
            h >= 2 for h in charge_hours
        ), "Should not charge hours 0 or 1 (outside 8h window)"

    def test_gap_too_small_no_charge(self):
        # Gap has only 1 entry but min_valley_slots=2 → skip
        entries = make_charge_entries(
            [1.0, 4.0, 4.0],
            modes_override=[None, "discharge", "discharge"],
        )
        _find_local_valleys(entries, 0.7, 120)  # need 2 slots, only 1 available
        assert entries[0]["mode"] == "standby"


# ---------------------------------------------------------------------------
# _extend_peaks
# ---------------------------------------------------------------------------


class TestExtendPeaks:

    def test_empty_list_no_crash(self):
        _extend_peaks([])  # Should not raise

    def test_all_standby_stays_standby(self):
        entries = make_timed_entries(["standby"] * 5)
        _extend_peaks(entries)
        assert all(e["mode"] == "standby" for e in entries)

    def test_head_filled_before_first_non_standby(self):
        # standby, standby, charge, standby, standby
        entries = make_timed_entries(
            ["standby", "standby", "charge", "standby", "standby"]
        )
        _extend_peaks(entries)
        assert entries[0]["mode"] == "discharge"  # head
        assert entries[1]["mode"] == "discharge"  # head
        assert entries[2]["mode"] == "charge"  # unchanged

    def test_tail_filled_after_last_non_standby(self):
        # discharge, standby, standby
        entries = make_timed_entries(["discharge", "standby", "standby"])
        _extend_peaks(entries)
        assert entries[1]["mode"] == "discharge"  # tail
        assert entries[2]["mode"] == "discharge"  # tail

    def test_gap_between_discharge_and_charge_filled(self):
        # discharge, standby, standby, standby, charge
        # gap condition is strict: entry.start > discharge.end
        # So entry[1] (start == discharge.end) stays standby; entries[2,3] become discharge
        entries = make_timed_entries(
            ["discharge", "standby", "standby", "standby", "charge"]
        )
        _extend_peaks(entries)
        assert entries[0]["mode"] == "discharge"
        assert entries[1]["mode"] == "standby"  # Boundary: not strictly inside gap
        assert entries[2]["mode"] == "discharge"
        assert entries[3]["mode"] == "discharge"
        assert entries[4]["mode"] == "charge"

    def test_charge_discharge_pair_not_filled(self):
        # charge→discharge: gap NOT filled (only discharge→charge triggers fill)
        entries = make_timed_entries(
            ["standby", "charge", "standby", "discharge", "standby"]
        )
        _extend_peaks(entries)
        # head: entry[0] → discharge (before first non-standby at index 1)
        assert entries[0]["mode"] == "discharge"
        # gap between charge(1) and discharge(3): no fill (wrong direction)
        assert entries[2]["mode"] == "standby"


# ---------------------------------------------------------------------------
# compute_charge_modes
# ---------------------------------------------------------------------------


class TestComputeChargeModes:

    @pytest.fixture(autouse=True)
    def _mock_tz(self):
        with patch(
            "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_default_time_zone",
            return_value=UTC,
        ):
            yield

    def test_empty_input_returns_empty(self):
        assert compute_charge_modes([], 0.7, 160, 240) == []

    def test_all_same_price_returns_all_standby(self):
        # Margin never met → no peaks/valleys → schedule stays in standby
        rates = make_rates([2.0] * 12)
        result = compute_charge_modes(rates, 0.7, 160, 240)
        assert len(result) == 12
        assert all(e["mode"] == "standby" for e in result)

    def test_spring_dst_day_keeps_all_slots_and_positive_durations(self):
        stockholm = ZoneInfo("Europe/Stockholm")
        hours = [0, 1] + list(range(3, 24))  # 02:00 is skipped on spring-forward day
        rates = [
            {
                "from": f"2024-03-31T{hour:02d}:00",
                "cost": 1.0 if i < 3 else 4.0,
            }
            for i, hour in enumerate(hours)
        ]

        with patch(
            "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_default_time_zone",
            return_value=stockholm,
        ):
            result = compute_charge_modes(rates, 0.7, 160, 240)

        mode_map = {entry["start"].strftime("%H:%M"): entry["mode"] for entry in result}
        assert len(result) == 23
        assert all(entry["end"] > entry["start"] for entry in result)
        assert result[1]["start"].strftime("%Y-%m-%dT%H:%M") == "2024-03-31T01:00"
        assert result[1]["end"].strftime("%Y-%m-%dT%H:%M") == "2024-03-31T03:00"
        # Current compact-rate parsing preserves the local 01:00→03:00 wall-clock
        # gap as a two-hour slot on spring-forward days.
        assert result[1]["end"] - result[1]["start"] == timedelta(hours=2)
        assert result[-1]["end"].strftime("%Y-%m-%dT%H:%M") == "2024-04-01T00:00"
        assert mode_map["00:00"] == "charge"
        assert mode_map["01:00"] == "charge"
        assert mode_map["03:00"] == "charge"
        assert mode_map["04:00"] == "discharge"
        assert mode_map["23:00"] == "discharge"

    def test_cheap_before_expensive_produces_charge_then_discharge(self):
        # hours 0-2: 1.0 (cheap → should charge)
        # hours 3-11: 4.0 (expensive → should discharge)
        costs = [1.0] * 3 + [4.0] * 9
        rates = make_rates(costs)
        result = compute_charge_modes(rates, 0.7, 160, 240)
        mode_map = {e["start"].hour: e["mode"] for e in result}

        assert mode_map[0] == "charge"
        assert mode_map[1] == "charge"
        assert mode_map[2] == "charge"
        assert mode_map[3] == "discharge"
        assert mode_map[7] == "discharge"
        assert mode_map[11] == "discharge"

    def test_result_entries_have_required_fields(self):
        rates = make_rates([1.0, 3.0, 1.0])
        result = compute_charge_modes(rates, 0.7, 60, 60)
        assert len(result) == 3
        for entry in result:
            assert "start" in entry
            assert "end" in entry
            assert "mode" in entry
            assert "cost" in entry
            assert entry["mode"] in ("standby", "charge", "discharge")

    def test_result_preserves_costs(self):
        costs = [1.0, 2.0, 3.0]
        rates = make_rates(costs)
        result = compute_charge_modes(rates, 0.7, 60, 60)
        result_costs = [e["cost"] for e in result]
        assert result_costs == costs

    def test_charge_always_before_discharge(self):
        # Charging must precede discharging — no charge entry should follow all discharge entries
        costs = [1.0] * 3 + [4.0] * 9
        rates = make_rates(costs)
        result = compute_charge_modes(rates, 0.7, 160, 240)
        last_charge = max(
            (i for i, e in enumerate(result) if e["mode"] == "charge"),
            default=-1,
        )
        first_discharge = min(
            (i for i, e in enumerate(result) if e["mode"] == "discharge"),
            default=len(result),
        )
        assert last_charge < first_discharge


# ---------------------------------------------------------------------------
# BatteryChargeModeSensor — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hass():
    h = MagicMock()
    h.config = MagicMock()
    h.config.time_zone = "UTC"
    return h


@pytest.fixture
def entry():
    e = MagicMock()
    e.entry_id = "test_entry_id"
    e.unique_id = "sensor.nord_pool_se4_current_price"
    e.options = {}
    return e


@pytest.fixture
def device_info():
    return MagicMock()


def test_constructor_uses_preferred_entity_id(entry, device_info, source_sensor):
    """The battery sensor should use the staged preferred entity ID."""
    sensor = BatteryChargeModeSensor(MagicMock(), entry, device_info, source_sensor)
    assert sensor.entity_id == "sensor.electricity_price_levels_battery_charge_mode"


@pytest.fixture
def source_sensor():
    sensor = MagicMock()
    sensor.has_rates = False
    sensor.compact_rates = []
    sensor.async_add_update_listener.return_value = MagicMock()
    return sensor


@pytest.fixture
def sensor(hass, entry, device_info, source_sensor):
    s = BatteryChargeModeSensor(hass, entry, device_info, source_sensor)
    s.hass = hass
    return s


# ---------------------------------------------------------------------------
# BatteryChargeModeSensor — lifecycle
# ---------------------------------------------------------------------------


@patch(
    "custom_components.electricitypricelevels.sensor.batterychargemodesensor.BatteryChargeModeSensor._start_sensor",
    new_callable=AsyncMock,
)
async def test_async_added_to_hass_starts_when_source_has_rates(
    mock_start, sensor, source_sensor
):
    source_sensor.has_rates = True
    await sensor.async_added_to_hass()
    mock_start.assert_awaited_once()
    source_sensor.async_add_update_listener.assert_called_once_with(
        sensor._handle_source_update
    )


@patch(
    "custom_components.electricitypricelevels.sensor.batterychargemodesensor.BatteryChargeModeSensor._start_sensor",
    new_callable=AsyncMock,
)
async def test_async_added_to_hass_waits_for_first_rates(
    mock_start, sensor, source_sensor
):
    await sensor.async_added_to_hass()
    mock_start.assert_not_awaited()
    source_sensor.async_add_update_listener.assert_called_once_with(
        sensor._handle_source_update
    )


async def test_handle_source_update_starts_sensor_on_first_rates(
    sensor, hass, source_sensor
):
    sensor._waiting_for_first_value = True
    source_sensor.has_rates = True
    created_tasks = []
    hass.async_create_task = lambda coro: created_tasks.append(
        asyncio.create_task(coro)
    )

    with patch.object(sensor, "_start_sensor", new_callable=AsyncMock) as mock_start:
        sensor._handle_source_update()
        await asyncio.gather(*created_tasks)

    mock_start.assert_awaited_once()


async def test_handle_source_update_refreshes_when_running(sensor, hass):
    sensor._waiting_for_first_value = False
    created_tasks = []
    hass.async_create_task = lambda coro: created_tasks.append(
        asyncio.create_task(coro)
    )

    with patch.object(
        sensor, "_refresh_from_source", new_callable=AsyncMock
    ) as mock_refresh:
        sensor._handle_source_update()
        await asyncio.gather(*created_tasks)

    mock_refresh.assert_awaited_once()


async def test_start_sensor_is_idempotent(sensor):
    sensor._waiting_for_first_value = False
    with patch.object(sensor, "_refresh_from_source", new_callable=AsyncMock):
        await sensor._start_sensor()
    assert sensor._task is None  # No task created because guard returned early


async def test_async_will_remove_cancels_task(sensor):
    sensor._task = MagicMock()
    await sensor.async_will_remove_from_hass()
    sensor._task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# BatteryChargeModeSensor — state, icon, attributes
# ---------------------------------------------------------------------------


def test_state_returns_current_mode(sensor):
    sensor._mode = "charge"
    assert sensor.state == "charge"


def test_zero_degradation_margin_is_preserved(hass, entry, device_info, source_sensor):
    entry.options = {"battery_degradation_cost": 0.0}
    battery_sensor = BatteryChargeModeSensor(hass, entry, device_info, source_sensor)

    assert battery_sensor._margin == 0.0


def test_sensor_uses_suggested_object_id_and_unrecorded_schedule(sensor):
    assert sensor._attr_suggested_object_id == "batterychargemode"
    assert "charge_entries" in sensor._unrecorded_attributes


@pytest.mark.parametrize(
    "mode,expected_icon",
    [
        ("standby", "mdi:battery-outline"),
        ("charge", "mdi:battery-charging"),
        ("discharge", "mdi:battery-arrow-down-outline"),
    ],
)
def test_icon_reflects_mode(mode, expected_icon, sensor):
    sensor._mode = mode
    assert sensor.icon == expected_icon


def test_extra_state_attributes_structure(sensor):
    start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    sensor._charge_entries = [
        {
            "start": start,
            "end": start + timedelta(hours=1),
            "mode": "charge",
            "cost": 1.0,
        }
    ]
    sensor._rebuild_cached_attributes()
    attrs = sensor.extra_state_attributes
    assert "charge_entries" in attrs
    assert "margin" in attrs
    assert "charging_time_minutes" in attrs
    assert "discharging_time_minutes" in attrs
    assert attrs["margin"] == MARGIN
    assert attrs["charging_time_minutes"] == CHARGING_TIME_MINUTES
    assert attrs["discharging_time_minutes"] == DISCHARGING_TIME_MINUTES


def test_extra_state_attributes_serialises_datetimes_to_strings(sensor):
    start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    sensor._charge_entries = [
        {
            "start": start,
            "end": start + timedelta(hours=1),
            "mode": "discharge",
            "cost": 3.0,
        }
    ]
    sensor._rebuild_cached_attributes()
    entry = sensor.extra_state_attributes["charge_entries"][0]
    assert isinstance(entry["start"], str)
    assert isinstance(entry["end"], str)
    assert entry["mode"] == "discharge"
    assert entry["cost"] == 3.0


async def test_refresh_from_source_writes_when_attributes_change_but_mode_stays_same(
    sensor,
):
    sensor._mode = "standby"
    sensor._cached_attributes = {"charge_entries": []}
    sensor.async_write_ha_state = MagicMock()

    def _fake_recompute():
        sensor._cached_attributes = {"charge_entries": [{"start": "2024-01-01T00:00"}]}
        return 60

    with patch.object(sensor, "_recompute", side_effect=_fake_recompute):
        await sensor._refresh_from_source()

    sensor.async_write_ha_state.assert_called_once()


# ---------------------------------------------------------------------------
# BatteryChargeModeSensor — _recompute / _update_current_mode
# ---------------------------------------------------------------------------


def test_recompute_returns_60_when_no_rates(sensor, source_sensor):
    source_sensor.compact_rates = []
    assert sensor._recompute() == 60


def test_recompute_populates_charge_entries(sensor, source_sensor):
    source_sensor.compact_rates = make_rates([1.0] * 3 + [4.0] * 9)
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=datetime(2024, 1, 1, 0, 30, tzinfo=UTC),
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_default_time_zone",
        return_value=UTC,
    ):
        sensor._recompute()
    assert len(sensor._charge_entries) == 12


def test_recompute_with_flat_prices_keeps_sensor_in_standby(sensor, source_sensor):
    source_sensor.compact_rates = make_rates([2.0] * 24)
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=datetime(2024, 1, 1, 12, 30, tzinfo=UTC),
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_default_time_zone",
        return_value=UTC,
    ):
        next_update = sensor._recompute()

    assert len(sensor._charge_entries) == 24
    assert all(entry["mode"] == "standby" for entry in sensor._charge_entries)
    assert sensor._mode == "standby"
    assert next_update == 1801


def test_recompute_handles_today_only_rates_without_tomorrow_data(sensor, source_sensor):
    source_sensor.compact_rates = make_rates([1.0] * 3 + [4.0] * 21)
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=datetime(2024, 1, 1, 23, 30, tzinfo=UTC),
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_default_time_zone",
        return_value=UTC,
    ):
        next_update = sensor._recompute()

    assert len(sensor._charge_entries) == 24
    assert sensor._charge_entries[-1]["start"] == datetime(2024, 1, 1, 23, 0, tzinfo=UTC)
    assert sensor._charge_entries[-1]["end"] == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert sensor._charge_entries[-1]["mode"] == "discharge"
    assert sensor._mode == "discharge"
    assert next_update == 1801


def test_update_current_mode_standby_when_no_entries(sensor):
    sensor._charge_entries = []
    result = sensor._update_current_mode()
    assert result == 60
    assert sensor._mode == "standby"


def test_update_current_mode_standby_when_no_matching_slot(sensor, hass):
    # now is outside all slots
    future_start = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    sensor._charge_entries = [
        {
            "start": future_start,
            "end": future_start + timedelta(hours=1),
            "mode": "charge",
            "cost": 1.0,
        }
    ]
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ):
        sensor._update_current_mode()
    assert sensor._mode == "standby"


@pytest.mark.parametrize(
    "current_hour,expected_mode",
    [
        (0, "charge"),
        (1, "discharge"),
        (2, "standby"),
    ],
)
def test_update_current_mode_picks_correct_slot(
    current_hour, expected_mode, sensor, hass
):
    base = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    sensor._charge_entries = [
        {
            "start": base,
            "end": base + timedelta(hours=1),
            "mode": "charge",
            "cost": 1.0,
        },
        {
            "start": base + timedelta(hours=1),
            "end": base + timedelta(hours=2),
            "mode": "discharge",
            "cost": 4.0,
        },
        {
            "start": base + timedelta(hours=2),
            "end": base + timedelta(hours=3),
            "mode": "standby",
            "cost": 2.0,
        },
    ]
    now = base + timedelta(hours=current_hour, minutes=30)
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=now,
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ):
        seconds_left = sensor._update_current_mode()
    assert sensor._mode == expected_mode
    assert seconds_left > 0


def test_update_current_mode_returns_seconds_until_slot_end(sensor, hass):
    base = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    sensor._charge_entries = [
        {
            "start": base,
            "end": base + timedelta(hours=1),
            "mode": "discharge",
            "cost": 4.0,
        }
    ]
    now = base + timedelta(minutes=30)  # 30 minutes into the slot
    with patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.now",
        return_value=now,
    ), patch(
        "custom_components.electricitypricelevels.sensor.batterychargemodesensor.dt_util.get_time_zone",
        return_value=UTC,
    ):
        seconds_left = sensor._update_current_mode()
    # 30 min remaining + 1 second buffer = 1801
    assert seconds_left == 1801


# ---------------------------------------------------------------------------
# BatteryChargeModeSensor — periodic update
# ---------------------------------------------------------------------------


@patch(
    "custom_components.electricitypricelevels.sensor.batterychargemodesensor.BatteryChargeModeSensor._refresh_from_source",
    return_value=0.01,
)
async def test_periodic_update_calls_refresh_and_sleeps(mock_refresh, sensor):
    sensor.platform = MagicMock()
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await sensor._periodic_update()
    mock_refresh.assert_called()
    mock_sleep.assert_called()
