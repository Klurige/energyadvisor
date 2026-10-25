"""Tests for the battery charge mode sensor."""

from __future__ import annotations

import asyncio
import datetime
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor.battery_planner import BatteryParams
from custom_components.energyadvisor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_MAX_SOC_PCT,
    CONF_BATTERY_MIN_SOC_PCT,
    CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
)
from custom_components.energyadvisor.sensor import batterychargemodesensor
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    DEFAULT_LOAD_KW,
    BatteryChargeModeSensor,
    ScheduleInputs,
    ScheduleSlot,
    build_schedule,
)

TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")
BASE = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
SOC_ENTITY = "sensor.battery_soc"
BATTERY_OPTIONS = {
    CONF_BATTERY_CAPACITY_KWH: 15.0,
    CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
    CONF_BATTERY_MIN_SOC_PCT: 0,
    CONF_BATTERY_MAX_SOC_PCT: 95,
}
PARAMS = BatteryParams(15.0, 0.0, 95.0, 5.0, 5.0)


def _slot(
    start: datetime.datetime,
    minutes: int,
    cost: float = 1.0,
    credit: float = 0.5,
) -> dict:
    return {
        "start": start,
        "end": start + datetime.timedelta(minutes=minutes),
        "cost": cost,
        "credit": credit,
    }


def _rates(costs: list[float], minutes: int = 15, start=BASE) -> list[dict]:
    """Back-to-back price slots, one per cost."""
    return [
        _slot(start + datetime.timedelta(minutes=minutes * i), minutes, cost)
        for i, cost in enumerate(costs)
    ]


def _valley_peak_rates() -> list[dict]:
    """Twelve cheap quarters followed by twelve expensive ones."""
    return _rates([1.0] * 12 + [3.0] * 12)


def _quarter_hour_rates() -> list[dict]:
    first = datetime.datetime(2026, 8, 15, 23, 45, tzinfo=TEST_TIMEZONE)
    return [
        {
            "start": first + datetime.timedelta(minutes=15 * i),
            "end": first + datetime.timedelta(minutes=15 * (i + 1)),
            "cost": 1.0 + i,
            "credit": 0.25 * i,
        }
        for i in range(4)
    ]


def _utc_slots(
    start_utc: datetime.datetime, costs: list[float], minutes: int = 15
) -> tuple[ScheduleSlot, ...]:
    """Local-time slots built with UTC arithmetic (valid across DST changes)."""
    slots = []
    for i, cost in enumerate(costs):
        start = (start_utc + datetime.timedelta(minutes=minutes * i)).astimezone(
            TEST_TIMEZONE
        )
        end = (start_utc + datetime.timedelta(minutes=minutes * (i + 1))).astimezone(
            TEST_TIMEZONE
        )
        slots.append(ScheduleSlot(start, end, cost, 0.0, 1.5))
    return tuple(slots)


def _schedule_slots(
    rates: list[dict], load_kw: float = 1.5
) -> tuple[ScheduleSlot, ...]:
    return tuple(
        ScheduleSlot(r["start"], r["end"], r["cost"], r["credit"], load_kw)
        for r in rates
    )


def _inputs(
    rates: list[dict],
    params: BatteryParams | None = PARAMS,
    soc: float = 0.0,
    now: datetime.datetime = BASE,
    horizon: float = 48.0,
) -> ScheduleInputs:
    return ScheduleInputs(_schedule_slots(rates), params, soc, now, horizon)


def _make_sensor(
    hass,
    rates: list[dict],
    options: dict | None = None,
    household: object | None = None,
    now: datetime.datetime = BASE,
) -> BatteryChargeModeSensor:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = "test_unique_id"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: True, **(options or {})}
    entry.runtime_data = SimpleNamespace(household_coordinator=household)

    price_sensor = MagicMock()
    price_sensor._rates = rates
    price_sensor.async_add_update_listener.return_value = MagicMock()

    sensor = BatteryChargeModeSensor(
        hass, entry, MagicMock(spec=DeviceInfo), price_sensor
    )
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()
    sensor._now = lambda: now
    return sensor


def _remove(sensor: BatteryChargeModeSensor) -> None:
    """Run (once) the callbacks the sensor registered with ``async_on_remove``."""
    callbacks = [call.args[0] for call in sensor.async_on_remove.call_args_list]
    sensor.async_on_remove.reset_mock()
    for callback in reversed(callbacks):
        callback()


async def _settle(sensor: BatteryChargeModeSensor) -> None:
    """Wait until the planner worker is idle."""
    while sensor._plan_task is not None and not sensor._plan_task.done():
        await sensor._plan_task


@pytest.fixture
async def make_sensor(hass):
    created: list[BatteryChargeModeSensor] = []

    def factory(*args, **kwargs) -> BatteryChargeModeSensor:
        sensor = _make_sensor(hass, *args, **kwargs)
        created.append(sensor)
        return sensor

    yield factory
    for sensor in created:
        _remove(sensor)
        await _settle(sensor)


def _modes(sensor: BatteryChargeModeSensor) -> list[str]:
    return [m["mode"] for m in sensor.extra_state_attributes["modes"]]


# -- build_schedule ---------------------------------------------------------


def test_without_a_battery_every_slot_is_standby_with_target_50() -> None:
    schedule = build_schedule(_inputs(_valley_peak_rates(), params=None))

    assert {m["mode"] for m in schedule.modes} == {"standby"}
    assert {m["target_soc"] for m in schedule.modes} == {50}
    assert schedule.current_mode == "standby"
    assert schedule.soc_trace == ()


def test_modes_have_matching_times_and_follow_the_price_slots() -> None:
    schedule = build_schedule(_inputs(_quarter_hour_rates(), params=None))

    assert [(m["from"], m["to"]) for m in schedule.modes] == [
        ("2026-08-15T23:45", "2026-08-16T00:00"),
        ("2026-08-16T00:00", "2026-08-16T00:15"),
        ("2026-08-16T00:15", "2026-08-16T00:30"),
        ("2026-08-16T00:30", "2026-08-16T00:45"),
    ]


def test_modes_carry_cost_and_credit_from_the_price_slots() -> None:
    schedule = build_schedule(_inputs(_quarter_hour_rates(), params=None))

    assert [m["cost"] for m in schedule.modes] == [1.0, 2.0, 3.0, 4.0]
    assert [m["credit"] for m in schedule.modes] == [0.0, 0.25, 0.5, 0.75]


def test_modes_round_cost_and_credit_to_two_decimals() -> None:
    schedule = build_schedule(
        _inputs([_slot(BASE, 15, cost=1.23456, credit=0.98765)], params=None)
    )

    assert schedule.modes == [
        {
            "from": "2026-08-15T00:00",
            "to": "2026-08-15T00:15",
            "mode": "standby",
            "target_soc": 50,
            "cost": 1.23,
            "credit": 0.99,
        }
    ]


def test_modes_follow_the_slot_length_of_the_price_slots() -> None:
    schedule = build_schedule(_inputs(_rates([1.0, 1.0], minutes=60), params=None))

    assert [(m["from"], m["to"]) for m in schedule.modes] == [
        ("2026-08-15T00:00", "2026-08-15T01:00"),
        ("2026-08-15T01:00", "2026-08-15T02:00"),
    ]


def test_no_slots_give_no_modes() -> None:
    schedule = build_schedule(_inputs([]))

    assert schedule.modes == []
    assert schedule.current_mode == "standby"


def test_battery_plan_charges_in_the_valley_and_discharges_on_the_peak() -> None:
    schedule = build_schedule(_inputs(_valley_peak_rates()))

    modes = [m["mode"] for m in schedule.modes]
    assert "charge" in modes[:12] and "discharge" not in modes[:12]
    assert "discharge" in modes[12:] and "charge" not in modes[12:]
    assert {m["cost"] for m in schedule.modes} == {1.0, 3.0}
    assert schedule.current_mode == modes[0]


def test_target_soc_is_the_planned_end_soc_rounded_to_one_decimal() -> None:
    schedule = build_schedule(_inputs(_valley_peak_rates(), soc=0.0))

    charge_targets = [m["target_soc"] for m in schedule.modes if m["mode"] == "charge"]
    assert charge_targets == sorted(charge_targets)
    assert max(charge_targets) <= 95.0
    assert all(target == round(target, 1) for target in charge_targets)
    # 1.25 kWh in a 15 kWh battery is 8.33 %, so rounding matters.
    assert [m["target_soc"] for m in schedule.modes[:2]] == [0.0, 0.0]
    assert any(target % 0.5 != 0 for target in charge_targets)
    assert len(schedule.soc_trace) == 24


def test_start_soc_is_clamped_into_the_battery_window() -> None:
    flat = _rates([1.0] * 4)
    params = BatteryParams(15.0, 10.0, 90.0, 5.0, 5.0)

    above = build_schedule(_inputs(flat, params=params, soc=100.0, now=BASE))
    below = build_schedule(_inputs(flat, params=params, soc=-5.0, now=BASE))

    # Past slots hold the (clamped) start SoC.
    late = BASE + datetime.timedelta(hours=1)
    assert {
        m["target_soc"]
        for m in build_schedule(_inputs(flat, params=params, soc=100.0, now=late)).modes
    } == {90.0}
    assert {
        m["target_soc"]
        for m in build_schedule(_inputs(flat, params=params, soc=-5.0, now=late)).modes
    } == {10.0}
    assert len(above.modes) == len(below.modes) == 4


def test_slots_that_ended_are_standby_with_the_start_soc_as_target() -> None:
    now = BASE + datetime.timedelta(hours=1)  # slots 0-3 have ended
    rates = _rates([0.1] * 4 + [1.0] * 8 + [3.0] * 12)

    schedule = build_schedule(_inputs(rates, soc=40.0, now=now))

    ended = schedule.modes[:4]
    assert {m["mode"] for m in ended} == {"standby"}
    assert {m["target_soc"] for m in ended} == {40.0}
    assert {m["cost"] for m in ended} == {0.1}


def test_current_slot_is_planned_for_its_remaining_time() -> None:
    rates = _rates([0.1, 5.0, 5.0], minutes=15)

    full = build_schedule(_inputs(rates, soc=0.0, now=BASE))
    one_minute_left = build_schedule(
        _inputs(rates, soc=0.0, now=BASE + datetime.timedelta(minutes=14))
    )
    last_seconds = build_schedule(
        _inputs(rates, soc=0.0, now=BASE + datetime.timedelta(minutes=14, seconds=50))
    )

    assert full.modes[0]["mode"] == "charge" and full.current_mode == "charge"
    # 5 kW for the remaining minute is 0.083 kWh, below the 0.125 kWh step.
    assert one_minute_left.modes[0]["mode"] == "standby"
    assert one_minute_left.current_mode == "standby"
    assert last_seconds.modes[0]["mode"] == "standby"


def test_slots_after_the_horizon_are_standby_and_not_planned() -> None:
    rates = _rates([1.0] * 12 + [3.0] * 12)  # peak starts three hours out
    now = BASE

    short = build_schedule(_inputs(rates, now=now, horizon=2.0))
    long = build_schedule(_inputs(rates, now=now, horizon=6.0))

    assert {m["mode"] for m in short.modes} == {"standby"}
    assert "discharge" in [m["mode"] for m in long.modes]


def test_slot_starting_exactly_at_the_horizon_is_not_planned() -> None:
    rates = _rates([1.0, 1.0, 1.0, 9.0])

    # The expensive slot starts 45 minutes out: with a 0.75 h horizon it is
    # outside, so charging for it would be pointless.
    outside = build_schedule(_inputs(rates, horizon=0.75, soc=0.0))
    inside = build_schedule(_inputs(rates, horizon=0.76, soc=0.0))

    assert {m["mode"] for m in outside.modes} == {"standby"}
    assert "discharge" in [m["mode"] for m in inside.modes]


def test_unplanned_slots_after_the_horizon_hold_the_last_planned_soc() -> None:
    rates = _rates([1.0] * 4 + [3.0] * 4 + [1.0] * 8)

    schedule = build_schedule(_inputs(rates, horizon=2.0, soc=0.0))

    tail = [m["target_soc"] for m in schedule.modes[8:]]
    assert len(set(tail)) == 1
    assert tail[0] == schedule.modes[7]["target_soc"]


def test_all_slots_in_the_past_stay_standby() -> None:
    now = BASE + datetime.timedelta(days=1)

    schedule = build_schedule(_inputs(_valley_peak_rates(), soc=33.33, now=now))

    assert {m["mode"] for m in schedule.modes} == {"standby"}
    assert {m["target_soc"] for m in schedule.modes} == {33.3}
    assert schedule.current_mode == "standby"


def test_repeated_hour_when_the_clocks_go_back_is_handled_by_timestamps() -> None:
    # 2026-10-25 02:00-03:00 happens twice (CEST, then CET).
    start_utc = datetime.datetime(2026, 10, 24, 23, 0, tzinfo=datetime.timezone.utc)
    costs = [3.0] * 4 + [0.1] * 4 + [3.0] * 4
    slots = _utc_slots(start_utc, costs)
    now = datetime.datetime(2026, 10, 25, 1, 30, tzinfo=datetime.timezone.utc)
    now = now.astimezone(TEST_TIMEZONE)  # 02:30 CET, the second time

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 0.0, now, 48.0))

    # The cheap first 02:00-03:00 hour is over, even though 02:30 has the same
    # wall-clock time as one of its slots.
    assert [m["mode"] for m in schedule.modes[:10]] == ["standby"] * 10
    assert schedule.modes[10]["from"] == "2026-10-25T02:30"
    assert schedule.current_mode == schedule.modes[10]["mode"]


def test_slot_that_ends_at_the_clock_change_keeps_its_real_length() -> None:
    start_utc = datetime.datetime(2026, 10, 24, 23, 0, tzinfo=datetime.timezone.utc)
    costs = [3.0] * 7 + [0.1, 5.0, 5.0, 5.0, 5.0]
    slots = _utc_slots(start_utc, costs)
    # Slot 7 is 02:45 CEST - 02:00 CET (wall clock goes backwards).
    assert slots[7].start.strftime("%H:%M") == "02:45"
    assert slots[7].end.strftime("%H:%M") == "02:00"
    now = datetime.datetime(2026, 10, 25, 0, 45, tzinfo=datetime.timezone.utc)

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 0.0, now, 48.0))

    assert schedule.modes[7]["mode"] == "charge"
    assert schedule.current_mode == "charge"


def test_100_slot_day_is_planned_without_error() -> None:
    start_utc = datetime.datetime(2026, 10, 24, 22, 0, tzinfo=datetime.timezone.utc)
    costs = [1.0 + (i % 24) / 10 for i in range(100)]
    slots = _utc_slots(start_utc, costs)

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 20.0, slots[0].start, 48.0))

    assert len(schedule.modes) == 100


# -- sensor: defaults, attributes, wiring ------------------------------------


def test_sensor_is_standby_with_empty_modes_before_any_plan(hass) -> None:
    sensor = _make_sensor(hass, [])

    assert sensor.state == "standby"
    assert sensor.icon == "mdi:battery-off"
    assert sensor.extra_state_attributes == {"modes": [], "current_soc_pct": None}


def test_modes_attribute_is_unrecorded(hass) -> None:
    assert "modes" in _make_sensor(hass, [])._unrecorded_attributes


async def test_added_to_hass_exposes_default_modes_and_listens_to_price_updates(
    make_sensor,
) -> None:
    sensor = make_sensor(_quarter_hour_rates(), options=BATTERY_OPTIONS)

    await sensor.async_added_to_hass()

    modes = sensor.extra_state_attributes["modes"]
    assert len(modes) == 4
    assert {m["mode"] for m in modes} == {"standby"}
    assert {m["target_soc"] for m in modes} == {50}
    sensor._price_sensor.async_add_update_listener.assert_called_once_with(
        sensor._handle_price_update
    )
    sensor.async_on_remove.assert_any_call(
        sensor._price_sensor.async_add_update_listener.return_value
    )


async def test_price_update_requests_a_plan_and_publishes_it(make_sensor) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    sensor.async_write_ha_state.reset_mock()

    sensor._price_sensor._rates = _valley_peak_rates()[:6]
    sensor._handle_price_update()
    await _settle(sensor)

    assert len(sensor.extra_state_attributes["modes"]) == 6
    sensor.async_write_ha_state.assert_called_once()


async def test_empty_without_price_data(make_sensor) -> None:
    sensor = make_sensor([], options=BATTERY_OPTIONS)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert sensor.extra_state_attributes["modes"] == []
    assert sensor.state == "standby"


async def test_slots_without_times_are_skipped(make_sensor) -> None:
    sensor = make_sensor([{"cost": 1.0}, _slot(BASE, 15)], options=BATTERY_OPTIONS)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert len(sensor.extra_state_attributes["modes"]) == 1


async def test_unconfigured_battery_is_standby_with_target_50(make_sensor) -> None:
    sensor = make_sensor(_valley_peak_rates())

    await sensor.async_added_to_hass()
    await _settle(sensor)

    modes = sensor.extra_state_attributes["modes"]
    assert len(modes) == 24
    assert {m["mode"] for m in modes} == {"standby"}
    assert {m["target_soc"] for m in modes} == {50}
    assert sensor.state == "standby"
    sensor.async_write_ha_state.assert_called()


@pytest.mark.parametrize(
    "options",
    [
        {CONF_BATTERY_MAX_CHARGE_POWER_W: 5000},
        {CONF_BATTERY_CAPACITY_KWH: 15.0},
        {**BATTERY_OPTIONS, CONF_BATTERY_CAPACITY_KWH: 0},
        {**BATTERY_OPTIONS, CONF_BATTERY_MAX_CHARGE_POWER_W: "abc"},
        {**BATTERY_OPTIONS, CONF_BATTERY_MIN_SOC_PCT: 95, CONF_BATTERY_MAX_SOC_PCT: 95},
    ],
)
def test_incomplete_battery_configuration_gives_no_params(hass, options) -> None:
    assert _make_sensor(hass, [], options=options)._battery_params() is None


def test_battery_params_use_defaults_and_convert_watts(hass) -> None:
    options = {
        CONF_BATTERY_CAPACITY_KWH: 10,
        CONF_BATTERY_MAX_CHARGE_POWER_W: "4000",
        CONF_BATTERY_DEGRADATION_COST: 0.7,
    }

    params = _make_sensor(hass, [], options=options)._battery_params()

    assert params == BatteryParams(
        capacity_kwh=10.0,
        min_soc_pct=5.0,
        max_soc_pct=100.0,
        max_charge_power_kw=4.0,
        max_discharge_power_kw=4.0,
        wear_cost_per_kwh=0.7,
    )


def test_battery_params_read_every_option(hass) -> None:
    options = {
        **BATTERY_OPTIONS,
        CONF_BATTERY_MAX_DISCHARGE_POWER_W: 3000,
        CONF_BATTERY_MIN_SOC_PCT: 10,
        CONF_BATTERY_MAX_SOC_PCT: 90,
        CONF_BATTERY_DEGRADATION_COST: 0.25,
    }

    params = _make_sensor(hass, [], options=options)._battery_params()

    assert params == BatteryParams(15.0, 10.0, 90.0, 5.0, 3.0, 0.9, 0.25)


@pytest.mark.parametrize("value", [None, "abc", -1.0, float("nan")])
def test_missing_or_invalid_degradation_cost_counts_as_zero(hass, value) -> None:
    options = {**BATTERY_OPTIONS, CONF_BATTERY_DEGRADATION_COST: value}

    params = _make_sensor(hass, [], options=options)._battery_params()

    assert params is not None and params.wear_cost_per_kwh == 0.0


# -- sensor: state, icon and plan content ------------------------------------


async def test_state_and_icon_follow_the_current_slot(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    options = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    sensor = make_sensor(_valley_peak_rates(), options=options)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    modes = _modes(sensor)
    assert modes[0] == "standby"
    charge_index = modes.index("charge")
    discharge_index = modes.index("discharge")
    cases = [
        ("standby", "0", 0, "mdi:battery-off"),
        ("charge", "0", charge_index, "mdi:battery-arrow-up"),
        # The battery has been charged by the time the peak starts.
        ("discharge", "95", discharge_index, "mdi:battery-arrow-down"),
    ]

    for mode, soc, index, icon in cases:
        hass.states.async_set(SOC_ENTITY, soc)
        await hass.async_block_till_done()
        sensor._now = lambda i=index: BASE + datetime.timedelta(minutes=15 * i + 1)
        sensor._request_plan()
        await _settle(sensor)
        assert _modes(sensor)[index] == mode
        assert sensor.state == mode
        assert sensor.icon == icon
        # Nothing before the current slot is planned.
        assert set(_modes(sensor)[:index]) <= {"standby"}


async def test_state_is_standby_when_no_slot_contains_now(make_sensor, hass) -> None:
    sensor = make_sensor(
        _valley_peak_rates(),
        options=BATTERY_OPTIONS,
        now=BASE + datetime.timedelta(days=2),
    )

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert sensor.state == "standby"
    assert set(_modes(sensor)) == {"standby"}


async def test_plan_uses_the_wear_cost_as_a_margin_of_loss(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    base = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    rates = _rates([1.0] * 12 + [1.8] * 12)

    cheap = make_sensor(rates, options=base)
    worn = make_sensor(rates, options={**base, CONF_BATTERY_DEGRADATION_COST: 0.7})
    for sensor in (cheap, worn):
        await sensor.async_added_to_hass()
        await _settle(sensor)

    assert "discharge" in _modes(cheap)
    assert set(_modes(worn)) == {"standby"}


async def test_horizon_option_truncates_the_plan(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    options = {
        **BATTERY_OPTIONS,
        CONF_BATTERY_SOC_ENTITY: SOC_ENTITY,
        CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS: 2,
    }
    sensor = make_sensor(_valley_peak_rates(), options=options)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert set(_modes(sensor)) == {"standby"}


def test_horizon_defaults_to_48_hours(hass) -> None:
    for value in (None, 0, -3, "x"):
        options = {**BATTERY_OPTIONS, CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS: value}
        sensor = _make_sensor(hass, [], options=options)

        assert sensor._build_inputs(BASE).horizon_hours == 48.0


# -- sensor: start SoC ---------------------------------------------------------


def _soc_sensor(hass, soc_state: str | None, now=BASE, options=None):
    if soc_state is not None:
        hass.states.async_set(SOC_ENTITY, soc_state)
    merged = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY, **(options or {})}
    return _make_sensor(hass, _valley_peak_rates(), options=merged, now=now)


def test_start_soc_comes_from_the_soc_entity(hass) -> None:
    sensor = _soc_sensor(hass, "42.5")

    inputs = sensor._build_inputs(BASE)

    assert inputs.start_soc_pct == 42.5
    assert sensor.extra_state_attributes["current_soc_pct"] == 42.5


def test_soc_entity_accepts_percent_signs_and_decimal_commas(hass) -> None:
    assert _soc_sensor(hass, "42,5 %")._build_inputs(BASE).start_soc_pct == 42.5


@pytest.mark.parametrize("state", ["unknown", "unavailable", "abc", "nan", None])
def test_unusable_soc_falls_back_to_min_soc_without_a_previous_plan(
    hass, state
) -> None:
    sensor = _soc_sensor(hass, state, options={CONF_BATTERY_MIN_SOC_PCT: 7})

    inputs = sensor._build_inputs(BASE)

    assert inputs.start_soc_pct == 7
    assert sensor.extra_state_attributes["current_soc_pct"] is None


def test_start_soc_is_not_clamped_by_the_sensor_but_by_the_plan(hass) -> None:
    sensor = _soc_sensor(hass, "99", options={CONF_BATTERY_MAX_SOC_PCT: 90})

    inputs = sensor._build_inputs(BASE)
    schedule = build_schedule(
        ScheduleInputs(inputs.slots, inputs.params, inputs.start_soc_pct, BASE, 48.0)
    )

    assert inputs.start_soc_pct == 99
    assert schedule.modes[0]["target_soc"] <= 90


async def test_unavailable_soc_uses_the_end_soc_of_the_previous_plan(
    make_sensor, hass
) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    sensor = _soc_sensor(hass, None)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    modes = sensor.extra_state_attributes["modes"]
    charging = [i for i, m in enumerate(modes) if m["mode"] == "charge"]
    assert charging

    hass.states.async_set(SOC_ENTITY, "unavailable")
    # The current slot is the one after the first charging slot.
    now = BASE + datetime.timedelta(minutes=15 * (charging[0] + 1) + 5)
    inputs = sensor._build_inputs(now)

    assert inputs.start_soc_pct == pytest.approx(
        modes[charging[0]]["target_soc"], abs=0.05
    )
    assert inputs.start_soc_pct > 0
    _remove(sensor)
    await _settle(sensor)


async def test_dead_reckoning_ignores_the_default_fixed_modes(hass) -> None:
    sensor = _soc_sensor(hass, "unavailable", options={CONF_BATTERY_MIN_SOC_PCT: 3})
    sensor._modes = sensor._default_modes()

    assert sensor._build_inputs(BASE + datetime.timedelta(hours=1)).start_soc_pct == 3


# -- sensor: load -------------------------------------------------------------


def _household(slots: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        forecast_slots=slots,
        register_update_callback=MagicMock(),
        unregister_update_callback=MagicMock(),
    )


def test_load_comes_from_the_household_forecast_slots(hass) -> None:
    forecast = [
        {"from": "2026-08-15T00:00", "load": 2.0},
        {"from": "2026-08-15T00:15", "load": 0.5},
        {"from": "2026-08-15T00:30", "load": 4.0},
    ]
    sensor = _make_sensor(hass, _rates([1.0] * 4), household=_household(forecast))

    loads = [slot.load_kw for slot in sensor._build_inputs(BASE).slots]

    assert loads == [2.0, 0.5, 4.0, DEFAULT_LOAD_KW]


def test_hourly_price_slots_average_the_forecast_slots_they_cover(hass) -> None:
    forecast = [
        {"from": "2026-08-15T00:00", "load": 1.0},
        {"from": "2026-08-15T00:15", "load": 2.0},
        {"from": "2026-08-15T00:30", "load": 3.0},
        {"from": "2026-08-15T00:45", "load": 4.0},
        {"from": "2026-08-15T01:00", "load": 6.0},
        {"from": "2026-08-15T01:15", "load": 8.0},
        # 02:00 is missing: the fallback applies.
        {"from": "bad", "load": 9.0},
        {"from": "2026-08-15T03:00", "load": "junk"},
        {"from": "2026-08-15T03:15", "load": float("nan")},
    ]
    sensor = _make_sensor(
        hass, _rates([1.0] * 4, minutes=60), household=_household(forecast)
    )

    loads = [slot.load_kw for slot in sensor._build_inputs(BASE).slots]

    assert loads == [2.5, 7.0, DEFAULT_LOAD_KW, DEFAULT_LOAD_KW]


def test_fallback_load_is_1_5_kw_without_a_household_coordinator(hass) -> None:
    assert DEFAULT_LOAD_KW == 1.5

    for household in (None, _household([])):
        sensor = _make_sensor(hass, _rates([1.0] * 2), household=household)

        assert [s.load_kw for s in sensor._build_inputs(BASE).slots] == [1.5, 1.5]


def test_negative_forecast_load_counts_as_zero(hass) -> None:
    forecast = [{"from": "2026-08-15T00:00", "load": -2.0}]
    sensor = _make_sensor(hass, _rates([1.0]), household=_household(forecast))

    assert sensor._build_inputs(BASE).slots[0].load_kw == 0.0


async def test_the_forecast_load_limits_the_discharge(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "95")
    options = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    rates = _rates([3.0] * 8)
    forecast = [
        {"from": slot["start"].strftime("%Y-%m-%dT%H:%M"), "load": load}
        for slot, load in zip(rates, [0.0] * 4 + [4.0] * 4)
    ]
    sensor = make_sensor(rates, options=options, household=_household(forecast))

    await sensor.async_added_to_hass()
    await _settle(sensor)

    modes = _modes(sensor)
    assert set(modes[:4]) == {"standby"}
    assert "discharge" in modes[4:]


async def test_household_updates_replan_and_the_callback_is_removed(
    make_sensor,
) -> None:
    household = _household([])
    sensor = make_sensor(
        _valley_peak_rates(), options=BATTERY_OPTIONS, household=household
    )

    await sensor.async_added_to_hass()
    await _settle(sensor)
    household.register_update_callback.assert_called_once_with(
        sensor._handle_household_update
    )
    sensor._request_plan = MagicMock()
    sensor._handle_household_update()
    sensor._request_plan.assert_called_once_with()

    household.unregister_update_callback.assert_not_called()
    _remove(sensor)
    household.unregister_update_callback.assert_called_once_with(
        sensor._handle_household_update
    )


# -- sensor: SoC gating ----------------------------------------------------------


async def test_soc_changes_below_two_points_are_stored_but_do_not_replan(
    make_sensor, hass
) -> None:
    sensor = _soc_sensor(hass, "50")
    sensor.async_on_remove = MagicMock()
    await sensor.async_added_to_hass()
    await _settle(sensor)
    assert sensor._last_planned_soc_pct == 50
    spy = MagicMock(wraps=sensor._request_plan)
    sensor._request_plan = spy

    hass.states.async_set(SOC_ENTITY, "51")
    await hass.async_block_till_done()

    spy.assert_not_called()
    assert sensor.extra_state_attributes["current_soc_pct"] == 51

    hass.states.async_set(SOC_ENTITY, "51.9")
    await hass.async_block_till_done()

    spy.assert_not_called()
    assert sensor.extra_state_attributes["current_soc_pct"] == 51.9

    # The gate compares with the SoC of the last plan, not the last event.
    hass.states.async_set(SOC_ENTITY, "52")
    await hass.async_block_till_done()

    spy.assert_called_once_with()
    assert sensor.extra_state_attributes["current_soc_pct"] == 52
    _remove(sensor)
    await _settle(sensor)


async def test_soc_drop_of_two_points_replans(hass) -> None:
    sensor = _soc_sensor(hass, "50")
    await sensor.async_added_to_hass()
    await _settle(sensor)
    spy = MagicMock(wraps=sensor._request_plan)
    sensor._request_plan = spy

    hass.states.async_set(SOC_ENTITY, "48")
    await hass.async_block_till_done()

    spy.assert_called_once_with()
    await _settle(sensor)
    assert sensor._last_planned_soc_pct == 48
    _remove(sensor)
    await _settle(sensor)


async def test_soc_becoming_unusable_is_stored_without_a_replan(hass) -> None:
    sensor = _soc_sensor(hass, "50")
    await sensor.async_added_to_hass()
    await _settle(sensor)
    spy = MagicMock(wraps=sensor._request_plan)
    sensor._request_plan = spy

    hass.states.async_set(SOC_ENTITY, "unavailable")
    await hass.async_block_till_done()

    spy.assert_not_called()
    assert sensor.extra_state_attributes["current_soc_pct"] is None

    # ... but a usable value after an unusable plan always replans.
    sensor._last_planned_soc_pct = None
    hass.states.async_set(SOC_ENTITY, "50")
    await hass.async_block_till_done()

    spy.assert_called_once_with()
    _remove(sensor)
    await _settle(sensor)


async def test_soc_entity_listener_is_removed(hass) -> None:
    sensor = _soc_sensor(hass, "50")
    await sensor.async_added_to_hass()
    await _settle(sensor)
    _remove(sensor)
    spy = MagicMock()
    sensor._request_plan = spy

    hass.states.async_set(SOC_ENTITY, "10")
    await hass.async_block_till_done()

    spy.assert_not_called()


# -- sensor: time trigger ------------------------------------------------------


async def test_quarter_hour_time_trigger_is_registered_and_removed(hass) -> None:
    sensor = _make_sensor(hass, _valley_peak_rates(), options=BATTERY_OPTIONS)
    unsubscribe = MagicMock()
    with patch.object(
        batterychargemodesensor, "async_track_time_change", return_value=unsubscribe
    ) as track:
        await sensor.async_added_to_hass()
        await _settle(sensor)

    track.assert_called_once_with(
        hass, sensor._handle_time_change, minute=(0, 15, 30, 45), second=0
    )
    sensor.async_on_remove.assert_any_call(unsubscribe)
    sensor._request_plan = MagicMock()
    sensor._handle_time_change(BASE)
    sensor._request_plan.assert_called_once_with()
    unsubscribe.assert_not_called()
    _remove(sensor)
    unsubscribe.assert_called_once_with()


async def test_time_trigger_rolls_the_plan_forward(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    options = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    sensor = make_sensor(_valley_peak_rates(), options=options)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    assert sensor.state == "standby"
    # Three hours later the battery is full and the peak has started.
    sensor._now = lambda: BASE + datetime.timedelta(hours=3, minutes=1)
    hass.states.async_set(SOC_ENTITY, "94")

    sensor._handle_time_change(sensor._now())
    await _settle(sensor)

    modes = sensor.extra_state_attributes["modes"]
    assert modes[12]["mode"] == "discharge"
    assert sensor.state == "discharge"


# -- sensor: worker ------------------------------------------------------------


async def test_overlapping_triggers_coalesce_and_the_last_inputs_win(
    make_sensor, hass
) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    started = threading.Event()
    release = threading.Event()
    solved_lengths: list[int] = []
    real_build_schedule = batterychargemodesensor.build_schedule

    def slow_build_schedule(inputs):
        solved_lengths.append(len(inputs.slots))
        started.set()
        assert release.wait(5)
        return real_build_schedule(inputs)

    with patch.object(batterychargemodesensor, "build_schedule", slow_build_schedule):
        sensor._request_plan()
        assert await hass.async_add_executor_job(started.wait, 5)
        first_task = sensor._plan_task
        # Three triggers while the first solve is running, the last one with
        # new prices.
        sensor._request_plan()
        sensor._request_plan()
        sensor._price_sensor._rates = _valley_peak_rates()[:6]
        sensor._request_plan()
        assert sensor._plan_task is first_task
        release.set()
        await _settle(sensor)

    assert solved_lengths == [24, 6]
    assert len(sensor.extra_state_attributes["modes"]) == 6
    # The result of the first solve was stale and is not published.
    sensor.async_write_ha_state.assert_called_once()


async def test_a_new_worker_starts_after_the_previous_one_finished(
    make_sensor,
) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    sensor._request_plan()
    first_task = sensor._plan_task
    await _settle(sensor)
    assert first_task.done()

    sensor._request_plan()
    await _settle(sensor)

    assert sensor._plan_task is not first_task
    assert sensor.async_write_ha_state.call_count == 2


async def test_planning_runs_in_the_executor(make_sensor, hass) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    threads: list[threading.Thread] = []
    real_build_schedule = batterychargemodesensor.build_schedule

    def recording_build_schedule(inputs):
        threads.append(threading.current_thread())
        return real_build_schedule(inputs)

    with patch.object(
        batterychargemodesensor, "build_schedule", recording_build_schedule
    ):
        sensor._request_plan()
        await _settle(sensor)

    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()


async def test_failed_plan_keeps_the_previous_modes_and_later_triggers_work(
    make_sensor, caplog
) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    before = sensor.extra_state_attributes["modes"]
    sensor.async_write_ha_state.reset_mock()

    with patch.object(
        batterychargemodesensor, "build_schedule", side_effect=RuntimeError("boom")
    ):
        sensor._request_plan()
        await _settle(sensor)

    assert "Battery charge mode planning failed" in caplog.text
    assert sensor.extra_state_attributes["modes"] is before
    sensor.async_write_ha_state.assert_not_called()

    sensor._request_plan()
    await _settle(sensor)
    sensor.async_write_ha_state.assert_called_once()


async def test_no_work_is_done_after_removal(make_sensor, hass) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    sensor.async_write_ha_state.reset_mock()
    spy = MagicMock(wraps=sensor._build_inputs)
    sensor._build_inputs = spy

    _remove(sensor)
    sensor._request_plan()
    sensor._handle_price_update()
    sensor._handle_household_update()
    sensor._handle_time_change(BASE)
    await asyncio.sleep(0)

    spy.assert_not_called()
    assert sensor._plan_task is None
    sensor.async_write_ha_state.assert_not_called()


async def test_removal_while_planning_discards_the_result(make_sensor, hass) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    started = threading.Event()
    release = threading.Event()
    real_build_schedule = batterychargemodesensor.build_schedule

    def slow_build_schedule(inputs):
        started.set()
        assert release.wait(5)
        return real_build_schedule(inputs)

    with patch.object(batterychargemodesensor, "build_schedule", slow_build_schedule):
        sensor._request_plan()
        assert await hass.async_add_executor_job(started.wait, 5)
        task = sensor._plan_task
        sensor.async_on_remove(sensor._stop_worker)
        _remove(sensor)
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    sensor.async_write_ha_state.assert_not_called()
