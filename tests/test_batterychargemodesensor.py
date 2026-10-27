"""Tests for the battery charge mode sensor."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import math
import random
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.helpers.sun import get_astral_event_date

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
    Schedule,
    ScheduleInputs,
    ScheduleSlot,
    SlotRecord,
    _parse_history,
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
SOC_OPTIONS = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
# Three cheap hours followed by three expensive ones, in 15-minute slots.
_VALLEY_THEN_PEAK = [1.0] * 12 + [3.0] * 12


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
    return _rates(_VALLEY_THEN_PEAK)


def _plan_modes(inputs: ScheduleInputs) -> list[str]:
    return [m["mode"] for m in build_schedule(inputs).modes]


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
) -> ScheduleInputs:
    return ScheduleInputs(_schedule_slots(rates), params, soc, now)


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


@pytest.fixture(autouse=True)
def sun():
    """No sunrise or sunset (polar night) unless a test sets ``side_effect``.

    The test installation is in San Diego, so real sun times would make
    "daytime" depend on the clock times the tests happen to use.
    """
    with patch.object(
        batterychargemodesensor, "get_astral_event_date", return_value=None
    ) as mock:
        yield mock


def _all_day(_hass, event: str, day: datetime.date) -> datetime.datetime:
    """Sunrise at midnight and sunset at the end of the day (local time)."""
    hour, minute = (0, 0) if event == "sunrise" else (23, 59)
    return datetime.datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=TEST_TIMEZONE
    ).astimezone(datetime.timezone.utc)


def _ts(rates: list[dict], index: int) -> float:
    return rates[index]["start"].timestamp()


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


def test_target_soc_is_the_planned_end_soc_rounded_up_to_whole_percent() -> None:
    schedule = build_schedule(_inputs(_valley_peak_rates(), soc=0.0))

    charge_targets = [m["target_soc"] for m in schedule.modes if m["mode"] == "charge"]
    assert charge_targets == sorted(charge_targets)
    assert max(charge_targets) <= 95.0
    assert all(type(target) is int for target in charge_targets)
    assert [m["target_soc"] for m in schedule.modes[:2]] == [0, 0]
    # The charge slots reach 5, 13.33, 21.67 and 30 %: whole values stay as
    # they are and the rest goes up.
    assert charge_targets == [5, 14, 22, 30]
    # The published value is never below the exact planned SoC, and less than
    # one point above it.
    for entry, (_, exact) in zip(schedule.modes, schedule.soc_trace):
        assert exact <= entry["target_soc"] < exact + 1
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
    assert {m["target_soc"] for m in ended} == {40}
    assert {m["cost"] for m in ended} == {0.1}


@pytest.mark.parametrize("seconds_in", [0, 300, 450, 600, 840, 870, 890, 899])
@pytest.mark.parametrize(
    ("costs", "soc", "mode"),
    [
        ([0.1, 5.0, 5.0], 0.0, "charge"),
        ([5.0, 5.0] + [1.0] * 10, 50.0, "discharge"),
    ],
)
def test_the_current_slot_keeps_its_mode_until_it_ends(
    costs: list[float], soc: float, mode: str, seconds_in: int
) -> None:
    now = BASE + datetime.timedelta(seconds=seconds_in)

    schedule = build_schedule(_inputs(_rates(costs), soc=soc, now=now))

    # A step of the planner's SoC grid (0.125 kWh) is more than the battery can
    # move in the last minutes of a slot; it must not turn them into standby.
    assert schedule.modes[0]["mode"] == mode
    assert schedule.current_mode == mode


def test_the_current_slots_target_covers_only_the_time_that_is_left() -> None:
    rates = _rates([5.0, 5.0] + [1.0] * 10)
    at = lambda seconds: build_schedule(  # noqa: E731
        _inputs(rates, soc=50.0, now=BASE + datetime.timedelta(seconds=seconds))
    )

    start, half, last = at(0), at(450), at(899)

    targets = lambda schedule: [
        m["target_soc"] for m in schedule.modes[:3]
    ]  # noqa: E731
    assert targets(start) == [47, 44, 40]  # 46.7, 43.3 and 40.0 rounded up
    # The load of half a slot (1.67 %) is drawn from here; later slots follow.
    assert targets(half) == [49, 45, 42]  # 48.3, 45.0 and 41.7 rounded up
    assert targets(last) == [50, 47, 44]  # 50.0, 46.7 and 43.3 rounded up


def test_the_schedule_reports_the_record_of_the_current_slot() -> None:
    rates = _valley_peak_rates()
    now = BASE + datetime.timedelta(minutes=20)  # slot 1

    schedule = build_schedule(_inputs(rates, soc=10.0, now=now))

    assert schedule.current == SlotRecord(
        _ts(rates, 1), schedule.current_mode, schedule.soc_trace[1][1]
    )
    assert build_schedule(_inputs(rates, params=None, now=now)).current is None
    outside = BASE + datetime.timedelta(days=2)
    assert build_schedule(_inputs(rates, now=outside)).current is None


def test_slots_that_ended_keep_the_mode_and_target_they_ran_with() -> None:
    rates = _rates([0.1] * 4 + [1.0] * 8 + [3.0] * 12)
    now = BASE + datetime.timedelta(hours=1)  # slots 0-3 have ended
    history = (
        SlotRecord(_ts(rates, 1), "charge", 12.5),
        SlotRecord(_ts(rates, 2), "maxuse", 9.0),
    )

    schedule = build_schedule(
        dataclasses.replace(_inputs(rates, soc=40.0, now=now), history=history)
    )

    assert [m["mode"] for m in schedule.modes[:4]] == [
        "standby",
        "charge",
        "maxuse",
        "standby",
    ]
    # A slot without a record carries the SoC on from the one before it. The
    # published targets are rounded up; the trace keeps the exact values.
    assert [m["target_soc"] for m in schedule.modes[:4]] == [40, 13, 9, 9]
    assert [target for _, target in schedule.soc_trace[:4]] == [40.0, 12.5, 9.0, 9.0]


def test_history_never_overrides_a_slot_that_is_still_planned() -> None:
    rates = _valley_peak_rates()
    now = BASE + datetime.timedelta(minutes=20)  # slots 1 and later are planned
    plain = _inputs(rates, soc=10.0, now=now)
    bogus = tuple(SlotRecord(_ts(rates, i), "discharge", 99.0) for i in range(1, 24))

    with_history = build_schedule(dataclasses.replace(plain, history=bogus))

    assert with_history.modes[1:] == build_schedule(plain).modes[1:]


# Daytime: sunrise to sunset, judged at the middle of the slot.


def _peak_slot_index() -> int:
    return _plan_modes(_inputs(_valley_peak_rates(), soc=50.0)).index("discharge")


def test_a_planned_discharge_in_daylight_is_maxuse_and_others_are_unchanged() -> None:
    rates = _valley_peak_rates()
    base = dataclasses.replace(_inputs(rates, soc=0.0), daylight=())
    night = build_schedule(base)
    day = build_schedule(
        dataclasses.replace(
            base,
            daylight=(
                (BASE.timestamp(), (BASE + datetime.timedelta(days=1)).timestamp()),
            ),
        )
    )

    night_modes = [m["mode"] for m in night.modes]
    assert "charge" in night_modes and "discharge" in night_modes
    expected = ["maxuse" if mode == "discharge" else mode for mode in night_modes]
    assert [m["mode"] for m in day.modes] == expected
    # Maxuse draws the battery down like discharge, so the plan is the same.
    assert day.soc_trace == night.soc_trace
    assert [m["target_soc"] for m in day.modes] == [
        m["target_soc"] for m in night.modes
    ]


def test_the_current_mode_is_maxuse_when_the_current_slot_discharges_by_day() -> None:
    now = BASE + datetime.timedelta(hours=3, minutes=1)  # first peak slot
    inputs = _inputs(_valley_peak_rates(), soc=50.0, now=now)
    daylight = ((BASE.timestamp(), (BASE + datetime.timedelta(days=1)).timestamp()),)

    night = build_schedule(inputs)
    day = build_schedule(dataclasses.replace(inputs, daylight=daylight))

    assert night.current_mode == "discharge"
    assert day.current_mode == "maxuse"
    assert day.current is not None and day.current.mode == "maxuse"


def test_daylight_is_judged_at_the_middle_of_the_slot() -> None:
    rates = _valley_peak_rates()
    index = _peak_slot_index()
    start = rates[index]["start"].timestamp()
    end = rates[index]["end"].timestamp()
    middle = (start + end) / 2

    def mode_with(daylight: tuple[tuple[float, float], ...]) -> str:
        inputs = dataclasses.replace(_inputs(rates, soc=50.0), daylight=daylight)
        return build_schedule(inputs).modes[index]["mode"]

    assert mode_with(((middle - 60, middle + 60),)) == "maxuse"
    assert mode_with(((middle - 60, middle),)) == "discharge"  # sunset at the middle
    assert mode_with(((middle + 1, end + 3600),)) == "discharge"  # sunrise after it
    assert mode_with(((start - 3600, middle + 1),)) == "maxuse"
    assert mode_with(((start - 7200, start - 3600), (middle - 1, end))) == "maxuse"


def test_charge_and_standby_are_not_changed_by_daylight() -> None:
    rates = _valley_peak_rates()
    inputs = dataclasses.replace(
        _inputs(rates, soc=0.0),
        daylight=((BASE.timestamp(), (BASE + datetime.timedelta(days=1)).timestamp()),),
    )

    modes = [m["mode"] for m in build_schedule(inputs).modes]

    assert "charge" in modes[:12] and "maxuse" not in modes[:12]
    assert "charge" not in modes[12:] and "maxuse" in modes[12:]


def _long_rates(slot_count: int) -> list[dict]:
    """Flat prices, then three cheap hours and three expensive ones at the end."""
    return _rates([2.0] * (slot_count - 24) + _VALLEY_THEN_PEAK)


@pytest.mark.parametrize("slot_count", [96, 192, 288])
def test_every_slot_of_a_one_two_or_three_day_array_is_planned(
    slot_count: int,
) -> None:
    modes = _plan_modes(_inputs(_long_rates(slot_count)))

    # With 288 slots the valley and the peak are more than 48 hours out, where
    # the old look-ahead cap left every slot on standby.
    assert len(modes) == slot_count
    assert "charge" in modes[-24:-12]
    assert "discharge" in modes[-12:]
    assert set(modes[:-24]) == {"standby"}


def test_a_plan_made_part_way_through_a_long_array_reaches_its_last_slot() -> None:
    now = BASE + datetime.timedelta(hours=12)  # the peak is 60 hours out

    modes = _plan_modes(_inputs(_long_rates(288), now=now))

    assert set(modes[:48]) == {"standby"}
    assert "charge" in modes[-24:-12]
    assert "discharge" in modes[-12:]


def test_the_last_slot_of_the_known_prices_is_planned() -> None:
    rates = _rates([2.0] * 300 + [1.0] * 4 + [3.0] * 4)

    schedule = build_schedule(_inputs(rates))

    assert "charge" in [m["mode"] for m in schedule.modes[300:304]]
    assert schedule.modes[-1]["mode"] == "discharge"
    assert schedule.modes[-1]["target_soc"] < schedule.modes[303]["target_soc"]


def test_planned_inputs_do_not_have_a_horizon() -> None:
    assert {f.name for f in dataclasses.fields(ScheduleInputs)} == {
        "slots",
        "params",
        "start_soc_pct",
        "now",
        "history",
        "daylight",
    }
    assert not hasattr(batterychargemodesensor, "DEFAULT_HORIZON_HOURS")
    assert not hasattr(
        batterychargemodesensor, "CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS"
    )


def test_all_slots_in_the_past_stay_standby() -> None:
    now = BASE + datetime.timedelta(days=1)

    schedule = build_schedule(_inputs(_valley_peak_rates(), soc=33.33, now=now))

    assert {m["mode"] for m in schedule.modes} == {"standby"}
    assert {m["target_soc"] for m in schedule.modes} == {34}
    assert schedule.current_mode == "standby"


def test_repeated_hour_when_the_clocks_go_back_is_handled_by_timestamps() -> None:
    # 2026-10-25 02:00-03:00 happens twice (CEST, then CET).
    start_utc = datetime.datetime(2026, 10, 24, 23, 0, tzinfo=datetime.timezone.utc)
    costs = [3.0] * 4 + [0.1] * 4 + [3.0] * 4
    slots = _utc_slots(start_utc, costs)
    now = datetime.datetime(2026, 10, 25, 1, 30, tzinfo=datetime.timezone.utc)
    now = now.astimezone(TEST_TIMEZONE)  # 02:30 CET, the second time

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 0.0, now))

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

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 0.0, now))

    assert schedule.modes[7]["mode"] == "charge"
    assert schedule.current_mode == "charge"


@pytest.mark.parametrize(
    ("start_utc", "slot_count"),
    [
        # Local midnight of the day the clocks go forward (23 h) and back (25 h).
        (datetime.datetime(2026, 3, 28, 23, 0, tzinfo=datetime.timezone.utc), 92),
        (datetime.datetime(2026, 10, 24, 22, 0, tzinfo=datetime.timezone.utc), 100),
    ],
)
def test_clock_change_days_are_planned_up_to_their_last_slot(
    start_utc: datetime.datetime, slot_count: int
) -> None:
    costs = [2.0] * (slot_count - 24) + _VALLEY_THEN_PEAK
    slots = _utc_slots(start_utc, costs)

    schedule = build_schedule(ScheduleInputs(slots, PARAMS, 0.0, slots[0].start))

    modes = [m["mode"] for m in schedule.modes]
    assert len(modes) == slot_count
    assert "charge" in modes[-24:-12]
    assert "discharge" in modes[-12:]
    assert set(modes[:-24]) == {"standby"}


# -- sensor: defaults, attributes, wiring ------------------------------------


def test_sensor_is_standby_with_empty_modes_before_any_plan(hass) -> None:
    sensor = _make_sensor(hass, [])

    assert sensor.state == "standby"
    assert sensor.icon == "mdi:battery-off"
    assert sensor.extra_state_attributes == {"modes": [], "target_soc": None}


def test_the_current_slots_target_soc_is_published_as_target_soc() -> None:
    now = BASE + datetime.timedelta(hours=1, minutes=5)

    schedule = build_schedule(_inputs(_valley_peak_rates(), soc=20.0, now=now))

    assert schedule.modes[4]["target_soc"] == schedule.current_target_soc
    assert schedule.current is not None
    assert schedule.current_target_soc == math.ceil(schedule.current.target_soc)


def test_target_soc_is_the_fixed_default_without_a_battery() -> None:
    now = BASE + datetime.timedelta(minutes=20)

    schedule = build_schedule(_inputs(_valley_peak_rates(), params=None, now=now))

    assert schedule.current is None
    assert schedule.current_target_soc == 50


def test_target_soc_is_none_when_no_slot_contains_now() -> None:
    after = BASE + datetime.timedelta(days=1)

    assert (
        build_schedule(_inputs(_valley_peak_rates(), now=after)).current_target_soc
        is None
    )
    assert build_schedule(_inputs([], now=BASE)).current_target_soc is None


async def test_target_soc_follows_the_plan_and_defaults_before_it(
    make_sensor, hass
) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    sensor = make_sensor(_valley_peak_rates(), options=SOC_OPTIONS)
    sensor._now = lambda: BASE + datetime.timedelta(minutes=15 * 3 + 1)

    await sensor.async_added_to_hass()
    assert sensor.extra_state_attributes["target_soc"] == 50
    await _settle(sensor)

    attributes = sensor.extra_state_attributes
    assert attributes["target_soc"] == attributes["modes"][3]["target_soc"]
    assert attributes["target_soc"] != 50
    assert "current_soc_pct" not in attributes
    _remove(sensor)
    await _settle(sensor)


async def test_target_soc_is_published_without_a_battery(make_sensor) -> None:
    sensor = make_sensor(_valley_peak_rates(), options={})

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert sensor.extra_state_attributes["target_soc"] == 50


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
    ran: dict[int, str] = {}

    for mode, soc, index, icon in cases:
        hass.states.async_set(SOC_ENTITY, soc)
        await hass.async_block_till_done()
        sensor._now = lambda i=index: BASE + datetime.timedelta(minutes=15 * i + 1)
        sensor._request_plan()
        await _settle(sensor)
        assert _modes(sensor)[index] == mode
        assert sensor.state == mode
        assert sensor.icon == icon
        # Slots before the current one are not planned: they keep the mode they
        # ran with, or are standby when they never ran.
        for earlier, ran_mode in ran.items():
            assert _modes(sensor)[earlier] == ran_mode
        assert {m for i, m in enumerate(_modes(sensor)[:index]) if i not in ran} <= {
            "standby"
        }
        ran[index] = mode


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


@pytest.mark.parametrize("horizon", [1, 24, 48, 96, 0, -3, "x"])
async def test_horizon_option_has_no_effect_on_the_modes(
    make_sensor, hass, horizon
) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    options = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    rates = _long_rates(288)
    unset = make_sensor(rates, options=options)
    configured = make_sensor(
        rates, options={**options, CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS: horizon}
    )

    for sensor in (unset, configured):
        await sensor.async_added_to_hass()
        await _settle(sensor)

    modes = _modes(unset)
    # The valley and the peak are 66 hours out: no horizon option may cut them.
    assert "charge" in modes[-24:-12] and "discharge" in modes[-12:]
    assert _modes(configured) == modes
    assert configured.extra_state_attributes == unset.extra_state_attributes


async def test_a_sensor_plans_all_slots_of_a_three_day_array(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    options = {**BATTERY_OPTIONS, CONF_BATTERY_SOC_ENTITY: SOC_ENTITY}
    sensor = make_sensor(_long_rates(288), options=options)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    modes = _modes(sensor)
    assert len(modes) == 288
    assert "charge" in modes[192:] and "discharge" in modes[192:]
    assert len(sensor._soc_trace) == 288


def test_solving_a_three_day_array_is_fast() -> None:
    rng = random.Random(7)
    rates = _rates([rng.uniform(0.5, 3.0) for _ in range(288)])
    inputs = ScheduleInputs(
        _schedule_slots(rates),
        BatteryParams(15.0, 5.0, 95.0, 5.0, 5.0, 0.9, 0.2),
        50.0,
        BASE,
    )

    started = time.perf_counter()
    schedule = build_schedule(inputs)
    elapsed = time.perf_counter() - started

    assert len(schedule.modes) == 288
    assert {m["mode"] for m in schedule.modes} == {"standby", "charge", "discharge"}
    assert elapsed < 3.0


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


def test_soc_entity_accepts_percent_signs_and_decimal_commas(hass) -> None:
    assert _soc_sensor(hass, "42,5 %")._build_inputs(BASE).start_soc_pct == 42.5


@pytest.mark.parametrize("state", ["unknown", "unavailable", "abc", "nan", None])
def test_unusable_soc_falls_back_to_min_soc_without_a_previous_plan(
    hass, state
) -> None:
    sensor = _soc_sensor(hass, state, options={CONF_BATTERY_MIN_SOC_PCT: 7})

    inputs = sensor._build_inputs(BASE)

    assert inputs.start_soc_pct == 7


def test_start_soc_is_not_clamped_by_the_sensor_but_by_the_plan(hass) -> None:
    sensor = _soc_sensor(hass, "99", options={CONF_BATTERY_MAX_SOC_PCT: 90})

    inputs = sensor._build_inputs(BASE)
    schedule = build_schedule(
        ScheduleInputs(inputs.slots, inputs.params, inputs.start_soc_pct, BASE)
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
    sensor._modes = sensor._default_schedule().modes

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


async def test_soc_changes_below_two_points_do_not_replan(make_sensor, hass) -> None:
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

    hass.states.async_set(SOC_ENTITY, "51.9")
    await hass.async_block_till_done()

    spy.assert_not_called()

    # The gate compares with the SoC of the last plan, not the last event.
    hass.states.async_set(SOC_ENTITY, "52")
    await hass.async_block_till_done()

    spy.assert_called_once_with()
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


async def test_soc_becoming_unusable_does_not_replan(hass) -> None:
    sensor = _soc_sensor(hass, "50")
    await sensor.async_added_to_hass()
    await _settle(sensor)
    spy = MagicMock(wraps=sensor._request_plan)
    sensor._request_plan = spy

    hass.states.async_set(SOC_ENTITY, "unavailable")
    await hass.async_block_till_done()

    spy.assert_not_called()

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


async def test_planning_runs_off_the_event_loop(make_sensor, hass) -> None:
    sensor = make_sensor(_long_rates(288), options=SOC_OPTIONS)
    hass.states.async_set(SOC_ENTITY, "0")
    loop = asyncio.get_running_loop()
    loop_ident = threading.get_ident()
    snapshot_idents: list[int] = []
    solve_idents: dict[str, int] = {}
    started = threading.Event()
    release = threading.Event()
    real_build_inputs = sensor._build_inputs
    real_build_schedule = batterychargemodesensor.build_schedule
    real_plan_battery = batterychargemodesensor.plan_battery

    def recording_build_inputs(now):
        snapshot_idents.append(threading.get_ident())
        return real_build_inputs(now)

    def recording_build_schedule(inputs):
        solve_idents["build_schedule"] = threading.get_ident()
        started.set()
        assert release.wait(5)
        return real_build_schedule(inputs)

    def recording_plan_battery(*args):
        solve_idents["plan_battery"] = threading.get_ident()
        return real_plan_battery(*args)

    sensor._build_inputs = recording_build_inputs
    with (
        patch.object(
            batterychargemodesensor, "build_schedule", recording_build_schedule
        ),
        patch.object(batterychargemodesensor, "plan_battery", recording_plan_battery),
    ):
        sensor._request_plan()
        assert await hass.async_add_executor_job(started.wait, 5)
        # The solve is blocked in its worker thread; the loop keeps running.
        responsive = asyncio.Event()
        loop.call_later(0.01, responsive.set)
        await asyncio.wait_for(responsive.wait(), 1)
        assert not sensor._plan_task.done()
        release.set()
        await _settle(sensor)

    # The inputs are snapshotted on the loop, the solve runs in another thread.
    assert snapshot_idents == [loop_ident]
    assert solve_idents.keys() == {"build_schedule", "plan_battery"}
    assert solve_idents["build_schedule"] != loop_ident
    assert solve_idents["plan_battery"] == solve_idents["build_schedule"]
    assert len(sensor.extra_state_attributes["modes"]) == 288


async def test_nothing_that_runs_on_the_loop_calls_the_planner(
    make_sensor, hass
) -> None:
    hass.states.async_set(SOC_ENTITY, "30")
    household = _household([{"from": "2026-08-15T00:00", "load": 2.0}])
    sensor = make_sensor(
        _long_rates(288),
        options=SOC_OPTIONS,
        household=household,
    )
    sensor._request_plan = MagicMock()
    forbidden = MagicMock(side_effect=AssertionError("planner called on the loop"))

    with patch.object(batterychargemodesensor, "plan_battery", forbidden):
        await sensor.async_added_to_hass()
        default_modes = sensor._default_schedule().modes
        inputs = sensor._build_inputs(BASE)
        sensor._handle_price_update()
        sensor._handle_household_update()
        sensor._handle_time_change(BASE)

    forbidden.assert_not_called()
    assert inputs.params is not None
    assert len(inputs.slots) == len(default_modes) == 288
    assert {m["mode"] for m in default_modes} == {"standby"}
    assert {m["target_soc"] for m in default_modes} == {50}
    assert sensor._modes == default_modes


def test_building_the_snapshot_is_cheap_for_a_three_day_array(hass) -> None:
    forecast = [
        {"from": slot["start"].strftime("%Y-%m-%dT%H:%M"), "load": 1.0}
        for slot in _long_rates(192)
    ]
    sensor = _make_sensor(
        hass,
        _long_rates(288),
        options=SOC_OPTIONS,
        household=_household(forecast),
    )
    hass.states.async_set(SOC_ENTITY, "30")

    started = time.perf_counter()
    inputs = sensor._build_inputs(BASE)
    sensor._default_schedule()
    elapsed = time.perf_counter() - started

    assert len(inputs.slots) == 288
    assert elapsed < 0.25


def test_the_snapshot_is_immutable_and_detached_from_its_sources(hass) -> None:
    rates = _long_rates(96)
    forecast = [{"from": "2026-08-15T00:00", "load": 2.0}]
    household = _household(forecast)
    hass.states.async_set(SOC_ENTITY, "30")
    sensor = _make_sensor(hass, rates, options=SOC_OPTIONS, household=household)

    inputs = sensor._build_inputs(BASE)
    expected = tuple(
        (s.start, s.end, s.cost, s.credit, s.load_kw) for s in inputs.slots
    )
    rates.clear()
    forecast.clear()
    sensor._price_sensor._rates = []

    assert isinstance(inputs.slots, tuple) and len(inputs.slots) == 96
    assert (
        tuple((s.start, s.end, s.cost, s.credit, s.load_kw) for s in inputs.slots)
        == expected
    )
    assert inputs.slots[0].load_kw == 2.0
    for frozen in (inputs, inputs.slots[0], inputs.params):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, "attribute", 0)
    assert all(
        isinstance(value, (datetime.datetime, float))
        for slot in inputs.slots
        for value in dataclasses.astuple(slot)
    )


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


# -- sensor: daytime maxuse ----------------------------------------------------


async def test_daytime_discharge_is_published_as_maxuse(make_sensor, hass, sun) -> None:
    sun.side_effect = _all_day
    hass.states.async_set(SOC_ENTITY, "95")
    probe = make_sensor(_valley_peak_rates(), options=SOC_OPTIONS)
    await probe.async_added_to_hass()
    await _settle(probe)
    first_peak = _modes(probe).index("maxuse")
    now = BASE + datetime.timedelta(minutes=15 * first_peak + 1)
    sensor = make_sensor(_valley_peak_rates(), options=SOC_OPTIONS, now=now)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert sensor.state == "maxuse"
    assert sensor.icon == "mdi:battery-sync"
    assert "discharge" not in _modes(sensor)
    sun.assert_any_call(hass, "sunrise", BASE.date())
    sun.assert_any_call(hass, "sunset", BASE.date())


async def test_discharge_at_night_stays_discharge(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "95")
    now = BASE + datetime.timedelta(hours=3, minutes=1)
    sensor = make_sensor(_valley_peak_rates(), options=SOC_OPTIONS, now=now)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    assert sensor.state == "discharge"
    assert sensor.icon == "mdi:battery-arrow-down"
    assert "maxuse" not in _modes(sensor)


def test_daylight_windows_cover_every_day_the_slots_start_on(hass, sun) -> None:
    sun.side_effect = _all_day
    next_day = BASE + datetime.timedelta(days=1)
    rates = _rates([1.0] * 2, start=BASE) + _rates([1.0] * 2, start=next_day)
    sensor = _make_sensor(hass, rates, options=BATTERY_OPTIONS)

    windows = sensor._build_inputs(BASE).daylight

    assert windows == tuple(
        (
            _all_day(hass, "sunrise", day).timestamp(),
            _all_day(hass, "sunset", day).timestamp(),
        )
        for day in (BASE.date(), next_day.date())
    )


@pytest.mark.parametrize(
    "sunrise_and_sunset", [(None, None), (None, "set"), ("rise", None)]
)
def test_a_day_without_sunrise_or_sunset_has_no_daylight(
    hass, sun, sunrise_and_sunset
) -> None:
    sun.side_effect = lambda _hass, event, day: (
        _all_day(_hass, event, day)
        if sunrise_and_sunset[0 if event == "sunrise" else 1]
        else None
    )
    sensor = _make_sensor(hass, _valley_peak_rates(), options=BATTERY_OPTIONS)

    assert sensor._build_inputs(BASE).daylight == ()


async def test_the_real_sun_helper_gives_a_summer_day_in_sweden(
    hass, monkeypatch
) -> None:
    monkeypatch.setattr(
        batterychargemodesensor, "get_astral_event_date", get_astral_event_date
    )
    hass.config.latitude = 59.33
    hass.config.longitude = 18.07
    hass.config.elevation = 20
    await hass.config.async_set_time_zone("Europe/Stockholm")
    sensor = _make_sensor(hass, _valley_peak_rates(), options=BATTERY_OPTIONS)

    ((sunrise, sunset),) = sensor._build_inputs(BASE).daylight

    rise = datetime.datetime.fromtimestamp(sunrise, TEST_TIMEZONE)
    set_ = datetime.datetime.fromtimestamp(sunset, TEST_TIMEZONE)
    assert rise.date() == set_.date() == BASE.date()
    assert 4 <= rise.hour <= 6 and 19 <= set_.hour <= 21


# -- sensor: modes of the slots that have run ------------------------------------


async def test_ended_slots_keep_the_mode_they_ran_with(make_sensor, hass) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    sensor = make_sensor(_valley_peak_rates(), options=SOC_OPTIONS)
    await sensor.async_added_to_hass()
    await _settle(sensor)
    charge_index = _modes(sensor).index("charge")
    sensor._now = lambda: BASE + datetime.timedelta(minutes=15 * charge_index + 1)
    sensor._request_plan()
    await _settle(sensor)
    ran_target = sensor.extra_state_attributes["modes"][charge_index]["target_soc"]

    hass.states.async_set(SOC_ENTITY, "95")
    sensor._now = lambda: BASE + datetime.timedelta(hours=3, minutes=1)
    sensor._request_plan()
    await _settle(sensor)

    ended = sensor.extra_state_attributes["modes"][charge_index]
    assert ended["mode"] == "charge"
    assert ended["target_soc"] == ran_target


async def test_apply_schedule_records_the_current_slot(make_sensor) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    schedule = Schedule([], "charge", (), SlotRecord(1000.0, "charge", 12.0))

    sensor._apply_schedule(schedule)

    assert sensor._history == {1000.0: SlotRecord(1000.0, "charge", 12.0)}
    sensor._apply_schedule(Schedule([], "standby", (), None))
    assert len(sensor._history) == 1
    sensor._apply_schedule(
        Schedule([], "maxuse", (), SlotRecord(1000.0, "maxuse", 9.0))
    )
    assert sensor._history == {1000.0: SlotRecord(1000.0, "maxuse", 9.0)}


async def test_records_older_than_two_days_are_dropped(make_sensor) -> None:
    sensor = make_sensor(_valley_peak_rates(), options=BATTERY_OPTIONS)
    now = BASE.timestamp()
    day = 24 * 3600
    for start in (now - 3 * day, now - 2 * day - 900, now - 2 * day, now - 900):
        sensor._history[start] = SlotRecord(start, "charge", 10.0)

    sensor._apply_schedule(Schedule([], "standby", (), SlotRecord(now, "standby", 5.0)))

    assert sorted(sensor._history) == [now - 2 * day, now - 900, now]


async def test_the_history_is_planned_with_and_survives_a_restart(
    make_sensor, hass
) -> None:
    hass.states.async_set(SOC_ENTITY, "0")
    rates = _valley_peak_rates()
    first = make_sensor(rates, options=SOC_OPTIONS)
    await first.async_added_to_hass()
    await _settle(first)
    charge_index = _modes(first).index("charge")
    first._now = lambda: BASE + datetime.timedelta(minutes=15 * charge_index + 1)
    first._request_plan()
    await _settle(first)
    saved = json.loads(json.dumps(first.extra_restore_state_data.as_dict()))

    now = BASE + datetime.timedelta(hours=3, minutes=1)
    second = make_sensor(rates, options=SOC_OPTIONS, now=now)
    second.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(saved))
    await second.async_added_to_hass()
    await _settle(second)

    assert second._history.keys() >= {_ts(rates, charge_index)}
    assert _modes(second)[charge_index] == "charge"


async def test_the_current_slot_is_saved_for_the_next_start(make_sensor) -> None:
    rates = _valley_peak_rates()
    sensor = make_sensor(rates, options=BATTERY_OPTIONS)

    await sensor.async_added_to_hass()
    await _settle(sensor)

    ((start, mode, target),) = sensor.extra_restore_state_data.as_dict()["history"]
    assert (start, mode) == (_ts(rates, 0), sensor.state)
    assert target == sensor.extra_state_attributes["modes"][0]["target_soc"]


def test_the_restore_data_lists_records_oldest_first(hass) -> None:
    sensor = _make_sensor(hass, [], options=BATTERY_OPTIONS)
    sensor._history = {
        20.0: SlotRecord(20.0, "maxuse", 1.5),
        10.0: SlotRecord(10.0, "charge", 2.5),
    }

    assert sensor.extra_restore_state_data.as_dict() == {
        "history": [[10.0, "charge", 2.5], [20.0, "maxuse", 1.5]]
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"history": None},
        {"history": "charge"},
        {"history": 5},
        {"history": [None, 3, "abc", [], [1.0], [1.0, "charge"], [1, 2, 3, 4]]},
        {"history": [[1.0, "bogus", 5.0], [2.0, ["charge"], 5.0]]},
        {
            "history": [
                ["x", "charge", 5.0],
                [None, "charge", 5.0],
                [True, "charge", 5.0],
            ]
        },
        {
            "history": [
                [1.0, "charge", "x"],
                [1.0, "charge", None],
                [1.0, "charge", True],
            ]
        },
        {"history": [[float("nan"), "charge", 5.0], [1.0, "charge", float("inf")]]},
    ],
)
def test_malformed_restore_data_is_ignored(payload) -> None:
    data = None if payload is None else RestoredExtraData(payload)

    assert _parse_history(data) == {}


def test_restoring_keeps_the_good_records_among_bad_ones() -> None:
    data = RestoredExtraData(
        {
            "history": [
                [1.0, "charge", 5.0],
                ["bad", "charge", 5.0],
                [2.0, "bogus", 5.0],
                [3.0, "maxuse", "7.5"],
                [4.0, "standby", 50],
            ]
        }
    )

    assert _parse_history(data) == {
        1.0: SlotRecord(1.0, "charge", 5.0),
        3.0: SlotRecord(3.0, "maxuse", 7.5),
        4.0: SlotRecord(4.0, "standby", 50.0),
    }
