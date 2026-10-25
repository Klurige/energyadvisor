"""Tests for the pure-Python battery planner."""

from __future__ import annotations

import itertools
import math
import random
import time

import pytest

from custom_components.energyadvisor.battery_planner import (
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_STANDBY,
    BatteryParams,
    PlanResult,
    PlanSlot,
    plan_battery,
)

SLOT_H = 0.25
LOAD_KWH = 1.5 * SLOT_H
ETA = math.sqrt(0.9)


def _params(**overrides) -> BatteryParams:
    values = {
        "capacity_kwh": 15.0,
        "min_soc_pct": 0.0,
        "max_soc_pct": 95.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "wear_cost_per_kwh": 0.0,
    }
    values.update(overrides)
    return BatteryParams(**values)


def _slots(
    prices: list[float], load_kwh: float = LOAD_KWH, hours: float = SLOT_H
) -> list[PlanSlot]:
    return [PlanSlot(hours, price, load_kwh) for price in prices]


def _runs(modes: list[str], mode: str) -> list[tuple[int, int]]:
    """Return (first index, length) of every run of ``mode``."""
    runs = []
    for key, group in itertools.groupby(enumerate(modes), key=lambda item: item[1]):
        if key == mode:
            items = list(group)
            runs.append((items[0][0], len(items)))
    return runs


def _execute(
    slots: list[PlanSlot], params: BatteryParams, start_soc: float, result: PlanResult
) -> float:
    """Independently price a plan from its modes and SoC trajectory."""
    eta = math.sqrt(params.round_trip_efficiency)
    previous = start_soc
    total = 0.0
    for slot, mode, end in zip(slots, result.modes, result.end_soc_pct):
        delta_kwh = (end - previous) * params.capacity_kwh / 100.0
        previous = end
        if mode == MODE_CHARGE:
            assert delta_kwh > 0
            total += slot.import_cost * (slot.load_kwh + delta_kwh / eta)
        elif mode == MODE_DISCHARGE:
            assert delta_kwh < 0
            delivered = min(-delta_kwh * eta, slot.load_kwh)
            total += slot.import_cost * (slot.load_kwh - delivered)
            total += params.wear_cost_per_kwh * delivered
        else:
            assert delta_kwh == pytest.approx(0, abs=1e-9)
            total += slot.import_cost * slot.load_kwh
    return total


def _valley_peak_prices(
    cheap: float = 1.0, dear: float = 3.0, count: int = 24
) -> list[float]:
    return [cheap] * count + [dear] * count


def test_empty_slots_give_an_empty_result() -> None:
    assert plan_battery([], _params(), 50.0) == PlanResult([], [], 0.0)


def test_flat_prices_stay_in_standby() -> None:
    result = plan_battery(_slots([1.5] * 48), _params(), 0.0)

    assert result.modes == [MODE_STANDBY] * 48
    assert result.end_soc_pct == [0.0] * 48
    assert result.total_cost == pytest.approx(1.5 * LOAD_KWH * 48)


def test_ties_prefer_standby_even_without_losses() -> None:
    params = _params(round_trip_efficiency=1.0)

    result = plan_battery(_slots([1.0, 1.0, 1.0, 1.0]), params, 0.0)

    assert result.modes == [MODE_STANDBY] * 4


def test_spread_below_losses_and_wear_stays_in_standby() -> None:
    # Delivered kWh costs cheap / 0.9 + wear = 1.611 with these prices.
    params = _params(wear_cost_per_kwh=0.5)
    prices = _valley_peak_prices(cheap=1.0, dear=1.55)

    result = plan_battery(_slots(prices, load_kwh=1.0), params, 0.0)

    assert result.modes == [MODE_STANDBY] * 48


def test_spread_above_losses_and_wear_charges_then_discharges() -> None:
    params = _params(wear_cost_per_kwh=0.5)
    prices = _valley_peak_prices(cheap=1.0, dear=1.8)

    result = plan_battery(_slots(prices, load_kwh=1.0), params, 0.0)

    assert MODE_CHARGE in result.modes
    assert MODE_DISCHARGE in result.modes
    assert result.modes.index(MODE_CHARGE) < result.modes.index(MODE_DISCHARGE)
    assert result.modes.index(MODE_CHARGE) < 24
    assert all(i < 24 for i, mode in enumerate(result.modes) if mode == MODE_CHARGE)
    assert all(i >= 24 for i, mode in enumerate(result.modes) if mode == MODE_DISCHARGE)
    baseline = sum(slot.import_cost * slot.load_kwh for slot in _slots(prices, 1.0))
    assert result.total_cost < baseline


def test_efficiency_and_wear_shift_the_decision() -> None:
    slots = _slots(_valley_peak_prices(cheap=1.0, dear=1.5), load_kwh=1.0)

    lossless = plan_battery(slots, _params(round_trip_efficiency=1.0), 0.0)
    lossy = plan_battery(slots, _params(round_trip_efficiency=0.6), 0.0)
    worn = plan_battery(slots, _params(wear_cost_per_kwh=0.6), 0.0)

    assert MODE_CHARGE in lossless.modes
    assert MODE_STANDBY == lossy.modes[0]
    assert set(lossy.modes) == {MODE_STANDBY}
    assert set(worn.modes) == {MODE_STANDBY}


def test_charge_window_is_limited_by_the_charge_power() -> None:
    # 14.25 kWh usable at 5 kW: 1.25 kWh per slot into the battery.
    slots = _slots(_valley_peak_prices(cheap=1.0, dear=3.0, count=40))

    result = plan_battery(slots, _params(), 0.0)

    charge_runs = _runs(result.modes, MODE_CHARGE)
    assert len(charge_runs) == 1
    assert charge_runs[0][1] == 12
    previous = 0.0
    for mode, soc in zip(result.modes, result.end_soc_pct):
        if mode == MODE_CHARGE:
            assert (soc - previous) * 15.0 / 100.0 <= 1.25 + 1e-9
        previous = soc
    assert max(result.end_soc_pct) == pytest.approx(95.0)


def test_discharge_is_limited_by_the_load_and_never_exports() -> None:
    slots = _slots(_valley_peak_prices(cheap=1.0, dear=3.0, count=40))

    result = plan_battery(slots, _params(), 0.0)

    discharged = 0.0
    delivered = 0.0
    previous = None
    for mode, soc in zip(result.modes, result.end_soc_pct):
        if previous is not None and mode == MODE_DISCHARGE:
            taken = (previous - soc) * 15.0 / 100.0
            discharged += taken
            delivered += min(taken * ETA, LOAD_KWH)
            # At most one step (0.125 kWh) more than the load is taken.
            assert taken * ETA <= LOAD_KWH + 0.125 * ETA + 1e-9
        previous = soc
    assert delivered <= LOAD_KWH * result.modes.count(MODE_DISCHARGE) + 1e-9
    # 14.25 kWh cannot be used up at 0.375 kWh per slot in a single slot.
    assert result.modes.count(MODE_DISCHARGE) >= 25


def test_soc_stays_within_bounds_and_follows_the_modes() -> None:
    rng = random.Random(7)
    prices = [rng.uniform(0.5, 3.0) for _ in range(96)]
    params = _params(min_soc_pct=10.0, max_soc_pct=90.0, wear_cost_per_kwh=0.1)

    result = plan_battery(_slots(prices), params, 40.0)

    previous = 40.0
    for mode, soc in zip(result.modes, result.end_soc_pct):
        assert 10.0 - 1e-9 <= soc <= 90.0 + 1e-9
        if mode == MODE_CHARGE:
            assert soc > previous
        elif mode == MODE_DISCHARGE:
            assert soc < previous
        else:
            assert soc == previous
        previous = soc
    assert MODE_CHARGE in result.modes and MODE_DISCHARGE in result.modes


def test_full_battery_never_charges() -> None:
    slots = _slots(_valley_peak_prices(cheap=1.0, dear=3.0))

    result = plan_battery(slots, _params(), 95.0)

    assert MODE_CHARGE not in result.modes
    assert MODE_DISCHARGE in result.modes
    assert result.end_soc_pct[0] == pytest.approx(95.0)


def test_start_soc_is_clamped_into_the_window() -> None:
    slots = _slots([1.0, 1.0], load_kwh=0.0)

    above = plan_battery(slots, _params(), 120.0)
    below = plan_battery(slots, _params(min_soc_pct=5.0), -10.0)

    assert above.end_soc_pct == pytest.approx([95.0, 95.0])
    assert below.end_soc_pct == pytest.approx([5.0, 5.0])


def test_empty_battery_with_the_only_peak_first_stays_in_standby() -> None:
    prices = [3.0] * 8 + [1.0] * 16

    result = plan_battery(_slots(prices), _params(), 0.0)

    assert result.modes == [MODE_STANDBY] * 24


def test_hourly_slots_charge_five_kwh_per_slot() -> None:
    prices = [1.0] * 6 + [3.0] * 12
    slots = _slots(prices, load_kwh=1.5, hours=1.0)

    result = plan_battery(slots, _params(), 0.0)

    # 14.25 kWh usable: 4.25 kWh and two full 5 kWh slots. Equal prices tie,
    # and ties keep standby, so charging is pushed to the latest slots.
    assert result.modes[:3] == [MODE_STANDBY] * 3
    assert result.modes[3:6] == [MODE_CHARGE] * 3
    assert result.end_soc_pct[3:6] == pytest.approx([4.25 / 0.15, 9.25 / 0.15, 95.0])
    assert MODE_DISCHARGE in result.modes[6:]


def test_short_slot_cannot_move_a_full_step() -> None:
    params = _params()
    cheap_full = [PlanSlot(SLOT_H, 0.1, 0.0), PlanSlot(SLOT_H, 5.0, 1.0)]
    cheap_short = [PlanSlot(1 / 60, 0.1, 0.0), PlanSlot(SLOT_H, 5.0, 1.0)]

    full = plan_battery(cheap_full, params, 0.0)
    short = plan_battery(cheap_short, params, 0.0)

    assert full.modes[0] == MODE_CHARGE
    # 5 kW for one minute is 0.083 kWh, less than the 0.125 kWh step.
    assert short.modes == [MODE_STANDBY, MODE_STANDBY]


def test_zero_load_slot_cannot_discharge() -> None:
    slots = [PlanSlot(SLOT_H, 1.0, 1.0), PlanSlot(SLOT_H, 9.0, 0.0)]

    result = plan_battery(slots, _params(), 95.0)

    assert result.modes[1] == MODE_STANDBY


def test_negative_prices_are_used_for_charging() -> None:
    slots = _slots([-1.0, -1.0, 1.0, 1.0], load_kwh=1.0)

    result = plan_battery(slots, _params(), 0.0)

    assert result.modes[0] == MODE_CHARGE
    assert result.modes[2] == MODE_DISCHARGE


def test_plan_is_deterministic() -> None:
    rng = random.Random(3)
    slots = _slots([rng.uniform(0.5, 3.0) for _ in range(48)])

    assert plan_battery(slots, _params(), 20.0) == plan_battery(slots, _params(), 20.0)


def test_total_cost_matches_an_independent_execution_of_the_plan() -> None:
    rng = random.Random(11)
    slots = _slots([rng.uniform(0.2, 3.0) for _ in range(96)])
    params = _params(wear_cost_per_kwh=0.3)

    result = plan_battery(slots, params, 30.0)

    assert _execute(slots, params, 30.0, result) == pytest.approx(result.total_cost)


def test_bad_input_is_guarded() -> None:
    slots = [
        PlanSlot(SLOT_H, math.nan, LOAD_KWH),
        PlanSlot(SLOT_H, 1.0, -5.0),
        PlanSlot(-1.0, 1.0, LOAD_KWH),
        PlanSlot(SLOT_H, 1.0, math.inf),
    ]

    result = plan_battery(slots, _params(wear_cost_per_kwh=-1.0), math.nan)

    assert len(result.modes) == 4
    assert result.modes[0] == MODE_STANDBY
    assert result.modes[2] == MODE_STANDBY
    # Only the third slot has a load and a price; the NaN price adds no cost.
    assert result.total_cost == pytest.approx(LOAD_KWH)


@pytest.mark.parametrize(
    "overrides",
    [
        {"capacity_kwh": 0.0},
        {"capacity_kwh": math.nan},
        {"min_soc_pct": 95.0},
        {"min_soc_pct": 99.0},
    ],
)
def test_battery_without_a_usable_window_holds_standby(overrides) -> None:
    slots = _slots(_valley_peak_prices())

    result = plan_battery(slots, _params(**overrides), 50.0)

    assert result.modes == [MODE_STANDBY] * 48
    assert len(set(result.end_soc_pct)) == 1
    assert result.total_cost == pytest.approx(
        sum(1.0 * LOAD_KWH for _ in range(24)) * 4
    )


def test_powers_of_zero_block_the_matching_action() -> None:
    slots = _slots(_valley_peak_prices(), load_kwh=1.0)

    no_charge = plan_battery(slots, _params(max_charge_power_kw=0.0), 0.0)
    no_discharge = plan_battery(slots, _params(max_discharge_power_kw=-1.0), 0.0)

    assert set(no_charge.modes) == {MODE_STANDBY}
    assert set(no_discharge.modes) == {MODE_STANDBY}


def _brute_force_cost(
    slots: list[PlanSlot], params: BatteryParams, start_soc: float
) -> float:
    """Cheapest cost over every action sequence in the same discretised model."""
    usable = params.capacity_kwh * (params.max_soc_pct - params.min_soc_pct) / 100.0
    states = min(160, max(1, round(usable / 0.125)))
    step = usable / states
    eta = math.sqrt(params.round_trip_efficiency)
    pct_per_state = (params.max_soc_pct - params.min_soc_pct) / states
    start = round((start_soc - params.min_soc_pct) / pct_per_state)

    options = []
    for slot in slots:
        actions = [(0, slot.import_cost * slot.load_kwh)]
        max_charge = math.floor(
            params.max_charge_power_kw * slot.duration_h / step + 1e-9
        )
        for k in range(1, max_charge + 1):
            actions.append((k, slot.import_cost * (slot.load_kwh + k * step / eta)))
        max_discharge = min(
            math.floor(params.max_discharge_power_kw * slot.duration_h / step + 1e-9),
            math.ceil(slot.load_kwh / (step * eta) - 1e-9),
        )
        for k in range(1, max_discharge + 1):
            delivered = min(k * step * eta, slot.load_kwh)
            cost = slot.import_cost * (slot.load_kwh - delivered)
            actions.append((-k, cost + params.wear_cost_per_kwh * delivered))
        options.append(actions)

    best = math.inf

    def search(index: int, state: int, cost: float) -> None:
        nonlocal best
        if index == len(options):
            best = min(best, cost)
            return
        for k, action_cost in options[index]:
            if 0 <= state + k <= states:
                search(index + 1, state + k, cost + action_cost)

    search(0, start, 0.0)
    return best


@pytest.mark.parametrize("seed", range(8))
def test_plan_is_optimal_against_exhaustive_search(seed: int) -> None:
    rng = random.Random(seed)
    # 128 kWh usable gives 160 states of 0.8 kWh, so few actions per slot.
    params = BatteryParams(
        capacity_kwh=160.0,
        min_soc_pct=10.0,
        max_soc_pct=90.0,
        max_charge_power_kw=rng.choice([1.7, 2.5, 3.3]),
        max_discharge_power_kw=rng.choice([1.7, 2.5, 3.3]),
        round_trip_efficiency=rng.choice([0.8, 0.9, 1.0]),
        wear_cost_per_kwh=rng.choice([0.0, 0.2, 0.6]),
    )
    slots = [
        PlanSlot(
            rng.choice([0.5, 1.0]),
            rng.uniform(-0.2, 3.0),
            rng.uniform(0.0, 2.5),
        )
        for _ in range(6)
    ]
    # Start close to the bounds so that they matter.
    start_soc = rng.choice([10.0, 12.5, 15.0, 85.0, 88.0, 90.0])

    result = plan_battery(slots, params, start_soc)

    assert result.total_cost == pytest.approx(
        _brute_force_cost(slots, params, start_soc), abs=1e-6
    )
    snapped = 10.0 + round((start_soc - 10.0) / 0.5) * 0.5
    assert _execute(slots, params, snapped, result) == pytest.approx(
        result.total_cost, abs=1e-6
    )


def _double_hump_price(hour: float) -> float:
    return (
        1.0
        + 0.5 * math.exp(-(((hour - 8) / 1.5) ** 2))
        + 1.4 * math.exp(-(((hour - 19) / 2.5) ** 2))
        - 0.5 * math.exp(-(((hour - 3) / 2) ** 2))
    )


def test_double_hump_day_charges_in_the_valley_and_discharges_over_the_peak() -> None:
    slots = _slots([_double_hump_price(i * SLOT_H) for i in range(96)])
    params = _params(wear_cost_per_kwh=0.2)

    result = plan_battery(slots, params, 0.0)

    charge_runs = _runs(result.modes, MODE_CHARGE)
    assert len(charge_runs) == 1
    first, length = charge_runs[0]
    # The valley is centred on 03:00 (slot 12); the block is 11-12 slots.
    assert 4 <= first and first + length <= 24
    assert 11 <= length <= 12
    discharge_runs = _runs(result.modes, MODE_DISCHARGE)
    longest_start, longest_length = max(discharge_runs, key=lambda run: run[1])
    # One long discharge block over the 19:00 peak (slot 76).
    assert longest_length >= 24
    assert longest_start <= 76 < longest_start + longest_length
    assert MODE_CHARGE not in result.modes[longest_start:]
    assert result.end_soc_pct[-1] == pytest.approx(0.0, abs=0.5)


def test_solving_192_slots_is_fast() -> None:
    rng = random.Random(1)
    slots = _slots([rng.uniform(0.5, 3.0) for _ in range(192)])

    started = time.perf_counter()
    result = plan_battery(slots, _params(wear_cost_per_kwh=0.2), 50.0)
    elapsed = time.perf_counter() - started

    assert len(result.modes) == 192
    assert elapsed < 2.0
