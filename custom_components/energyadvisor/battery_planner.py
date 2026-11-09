"""Battery schedule planner: a dynamic programme over state of charge.

The planner has no Home Assistant dependency and is pure Python. It takes a
chronological list of slots (duration, grid price, household load and solar
surplus) and decides for each slot whether the battery should charge from the
grid or solar, discharge into the household, sell to the grid or stay idle, so
that the total grid cost (net of exported energy) is as low as possible, subject
to any selected charging margin and export reserve.

Model:
- The usable energy window is ``capacity * (max_soc - min_soc)``. The state of
  charge is discretised in at most 160 equal energy steps of at least 0.125 kWh
  (the step is stretched slightly so that the top state is exactly ``max_soc``).
- ``charge`` moves ``k`` steps into the battery. Solar surplus is used first;
  the grid delivers any remaining charge energy, in addition to the household
  load. ``k`` is limited by the charge power and the room left in the battery.
  ``pvcharge`` stores surplus solar only, giving up the export credit for the
  energy stored.
- ``discharge`` takes ``k`` steps out of the battery and delivers
  ``min(k * step * eta_discharge, load)`` to the household. The grid delivers
  the rest of the load. ``discharge`` never exports. ``k`` is limited by the
  discharge power, the load and the energy left in the battery.
- ``sell`` takes ``k`` steps out of the battery, like ``discharge``, but so
  much that the household load is covered and the surplus
  ``k * step * eta_discharge - load`` is exported to the grid at the slot's
  ``export_credit`` per kWh. Nothing is imported while selling. ``k`` is limited
  by the discharge power (there is no separate export limit) and the energy
  left in the battery. A slot without a positive finite ``export_credit`` never
  sells.
- ``standby`` leaves the battery alone; the grid delivers any load not covered
  by solar.
- Each kWh the battery delivers (to the household and to the grid) adds
  ``wear_cost_per_kwh``. A kWh bought at price ``c`` and sold again therefore
  only pays when ``export_credit > c / round_trip_efficiency +
  wear_cost_per_kwh``.
- Energy left in the battery after the last slot (the end of the known prices)
  is worth ``terminal_value_per_kwh`` per stored kWh. With the default of zero
  the battery is used up by the end; callers that re-plan regularly (rolling
  horizon) set it to what the energy will be worth once new prices arrive, so
  that the battery is not emptied just because the known prices end.
- The number of slots is not limited. The work grows linearly with it.
- Ties go to ``standby``, and otherwise to the smaller action.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

MODE_STANDBY = "standby"
MODE_CHARGE = "charge"
MODE_PVCHARGE = "pvcharge"
MODE_DISCHARGE = "discharge"
MODE_SELL = "sell"

MIN_STEP_KWH = 0.125
MAX_STATES = 160
DEFAULT_ROUND_TRIP_EFFICIENCY = 0.9
# An action must beat standby by more than this to be chosen.
TIE_EPSILON = 1e-9
# Absorbs float noise when rounding power / step ratios down or up.
LIMIT_EPSILON = 1e-9
# Discount forecast solar's export value so uncertain forecasts favor storage.
SOLAR_EXPORT_CREDIT_FACTOR = 0.8


@dataclass(frozen=True)
class BatteryParams:
    """Battery hardware and cost parameters.

    ``round_trip_efficiency`` is split evenly over charging and discharging
    (``eta = sqrt(round_trip_efficiency)``). ``wear_cost_per_kwh`` is the cost
    of each kWh the battery delivers, in the same currency as the slot prices.
    ``terminal_value_per_kwh`` is what each kWh stored in the battery above the
    min SoC is worth after the last slot (0: it is worth nothing).
    ``charge_to_max`` extends selected grid-charge periods into preceding
    standby slots at no higher price and charges at full available power.
    ``sell_reserve_fraction`` protects that fraction of the usable SoC window
    from export; household discharge can still use it.
    """

    capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY
    wear_cost_per_kwh: float = 0.0
    terminal_value_per_kwh: float = 0.0
    charge_to_max: bool = False
    sell_reserve_fraction: float = 0.0


@dataclass(frozen=True)
class PlanSlot:
    """One planning slot.

    ``import_cost`` is the price per kWh imported from the grid (fees and VAT
    included), ``load_kwh`` the household energy still to be supplied after
    direct solar use, and ``export_credit`` the revenue per kWh exported to the
    grid (all taxes and fees included). ``solar_surplus_kwh`` is forecast solar
    energy available after the household load is covered. NaN export credit,
    which is the default, means that nothing can be sold in the slot.
    """

    duration_h: float
    import_cost: float
    load_kwh: float
    export_credit: float = math.nan
    solar_surplus_kwh: float = 0.0


@dataclass(frozen=True)
class PlanResult:
    """The plan: one mode and one planned end-of-slot SoC (%) per slot.

    ``total_cost`` is the grid cost plus wear minus the revenue of the exported
    energy over all slots, minus the terminal value of the energy left in the
    battery at the end. ``allow_solar_charging`` identifies household-discharge
    slots where storing an extra solar energy step at the planned end SoC is
    worth at least its discounted export credit, using the remaining plan.
    """

    modes: list[str]
    end_soc_pct: list[float]
    total_cost: float
    allow_solar_charging: list[bool] = field(default_factory=list)


def plan_battery(
    slots: Sequence[PlanSlot],
    params: BatteryParams,
    start_soc_pct: float,
    continuing_charge_slots: frozenset[int] = frozenset(),
) -> PlanResult:
    """Return a battery plan for ``slots``.

    Inputs:
        - ``slots`` in chronological order.
        - Battery ``params`` and the state of charge at the start of the first
          slot (clamped into the allowed window and snapped to the nearest
          step).
        - ``continuing_charge_slots`` identifies an already-started charging
          period that should be completed when ``charge_to_max`` is enabled.
    Outputs:
        - A ``PlanResult`` with one entry per slot. No slots give an empty
          result.
    Bad input is handled as follows: a negative or non-finite load counts as
    zero, a negative or non-finite duration as zero (no battery action), and a
    slot with a non-finite price allows no battery action and adds no cost.
    Negative prices are valid. Non-finite or negative powers count as zero,
    the efficiency is clamped into 0.01..1 and a negative or non-finite wear
    cost or terminal value counts as zero. A slot whose export credit is not
    finite or not positive never sells. When the battery has no usable energy
    window every slot is ``standby`` and the SoC stays where it is. With
    ``charge_to_max``, the cheapest plan identifies charging periods, then a
    second plan fills the battery in those periods when power and time allow.
    """
    result = _plan_battery(slots, params, start_soc_pct, frozenset())
    if not params.charge_to_max:
        return result

    capacity = _finite(params.capacity_kwh, 0.0)
    usable_kwh = (
        capacity
        * (_finite(params.max_soc_pct, 0.0) - _finite(params.min_soc_pct, 0.0))
        / 100.0
    )
    if capacity <= 0 or usable_kwh <= 0:
        return result
    states = min(MAX_STATES, max(1, round(usable_kwh / MIN_STEP_KWH)))
    step = usable_kwh / states
    charge_kw = max(_finite(params.max_charge_power_kw, 0.0), 0.0)

    def can_charge(index: int) -> bool:
        return (
            0 <= index < len(slots)
            and math.isfinite(_finite(slots[index].import_cost, math.nan))
            and charge_kw * _finite(slots[index].duration_h, 0.0) / step + LIMIT_EPSILON
            >= 1
        )

    forced = {i for i, mode in enumerate(result.modes) if mode == MODE_CHARGE}
    forced.update(i for i in continuing_charge_slots if can_charge(i))
    for index in sorted(forced):
        price = _finite(slots[index].import_cost, math.nan)
        previous = index - 1
        while (
            previous >= 0
            and result.modes[previous] == MODE_STANDBY
            and can_charge(previous)
            and _finite(slots[previous].import_cost, math.inf) <= price
        ):
            forced.add(previous)
            previous -= 1
    if not forced:
        return result
    return _plan_battery(slots, params, start_soc_pct, frozenset(forced))


def _plan_battery(
    slots: Sequence[PlanSlot],
    params: BatteryParams,
    start_soc_pct: float,
    forced_charge: frozenset[int],
) -> PlanResult:
    """Solve the plan, optionally charging at maximum power in selected slots."""
    count = len(slots)
    if not count:
        return PlanResult([], [], 0.0)

    prices = [_finite(slot.import_cost, math.nan) for slot in slots]
    loads = [max(_finite(slot.load_kwh, 0.0), 0.0) for slot in slots]
    durations = [
        max(_finite(slot.duration_h, 0.0), 0.0) if math.isfinite(price) else 0.0
        for slot, price in zip(slots, prices)
    ]
    prices = [price if math.isfinite(price) else 0.0 for price in prices]
    credits = [_finite(slot.export_credit, math.nan) for slot in slots]
    solar_surplus = [max(_finite(slot.solar_surplus_kwh, 0.0), 0.0) for slot in slots]
    solar_export_credit = [
        credit if math.isfinite(credit) and credit > 0 else 0.0 for credit in credits
    ]
    capacity = _finite(params.capacity_kwh, 0.0)
    low = _finite(params.min_soc_pct, 0.0)
    high = _finite(params.max_soc_pct, 0.0)
    usable_kwh = capacity * (high - low) / 100.0
    if capacity <= 0 or usable_kwh <= 0:
        soc = _finite(start_soc_pct, low)
        if low <= high:
            soc = min(max(soc, low), high)
        return PlanResult(
            [MODE_STANDBY] * count,
            [soc] * count,
            sum(
                price * load - credit * surplus
                for price, load, credit, surplus in zip(
                    prices, loads, solar_export_credit, solar_surplus
                )
            ),
            [False] * count,
        )

    states = min(MAX_STATES, max(1, round(usable_kwh / MIN_STEP_KWH)))
    step = usable_kwh / states
    pct_per_state = (high - low) / states
    eta = math.sqrt(min(max(_finite(params.round_trip_efficiency, 0.9), 0.01), 1.0))
    wear = max(_finite(params.wear_cost_per_kwh, 0.0), 0.0)
    terminal_value = max(_finite(params.terminal_value_per_kwh, 0.0), 0.0)
    max_charge_kw = max(_finite(params.max_charge_power_kw, 0.0), 0.0)
    max_discharge_kw = max(_finite(params.max_discharge_power_kw, 0.0), 0.0)
    sell_reserve = math.ceil(
        states * min(max(_finite(params.sell_reserve_fraction, 0.0), 0.0), 1.0)
        - LIMIT_EPSILON
    )

    start_soc = min(max(_finite(start_soc_pct, low), low), high)
    start_state = round((start_soc - low) / pct_per_state)

    charge_steps = [
        min(states, math.floor(max_charge_kw * hours / step + LIMIT_EPSILON))
        for hours in durations
    ]
    pvcharge_steps = [
        min(
            charge_steps[t],
            math.floor(surplus * eta / step + LIMIT_EPSILON),
        )
        for t, surplus in enumerate(solar_surplus)
    ]
    discharge_steps = [
        min(
            states,
            math.floor(max_discharge_kw * hours / step + LIMIT_EPSILON),
            math.ceil(load / (step * eta) - LIMIT_EPSILON),
        )
        for hours, load in zip(durations, loads)
    ]
    # A sell must take out more than the load, so that something is exported.
    sell_min_steps = [
        math.floor(load / (step * eta) + LIMIT_EPSILON) + 1 for load in loads
    ]
    sell_max_steps = [
        (
            min(states, math.floor(max_discharge_kw * hours / step + LIMIT_EPSILON))
            if math.isfinite(credit) and credit > 0
            else 0
        )
        for hours, credit in zip(durations, credits)
    ]

    # choices[t][s]: steps charged (> 0) or taken out of the battery (< 0) in
    # slot t at state s; sold[t][s] tells a sell from a discharge.
    choices: list[list[int]] = [[]] * count
    sold: list[list[bool]] = [[]] * count
    pv_charged: list[list[bool]] = [[]] * count
    solar_charging: list[list[bool]] = [[]] * count
    next_value = [0.0 - terminal_value * s * step for s in range(states + 1)]
    for t in range(count - 1, -1, -1):
        price = prices[t]
        load = loads[t]
        surplus = solar_surplus[t]
        export_credit = solar_export_credit[t]
        value = [price * load - export_credit * surplus + v for v in next_value]
        if t in forced_charge:
            value[:-1] = [math.inf] * states
        choice = [0] * (states + 1)
        pvcharged = [False] * (states + 1)

        for k in range(1, charge_steps[t] + 1):
            charge_input = k * step / eta
            if charge_input <= surplus + LIMIT_EPSILON:
                continue
            grid_charge = charge_input - surplus
            cost = price * (load + grid_charge)
            for s, future in enumerate(next_value[k:]):
                if t in forced_charge and k != min(charge_steps[t], states - s):
                    continue
                total = cost + future
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = k
                    pvcharged[s] = False

        for k in range(1, pvcharge_steps[t] + 1):
            charge_input = k * step / eta
            solar_used = min(charge_input, surplus)
            exported = max(surplus - charge_input, 0.0)
            credited_export = exported + solar_used * (1 - SOLAR_EXPORT_CREDIT_FACTOR)
            cost = price * load - export_credit * credited_export
            for s, future in enumerate(next_value[k:]):
                if t in forced_charge and k != min(charge_steps[t], states - s):
                    continue
                total = cost + future
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = k
                    pvcharged[s] = True

        for k in range(1, 1 if t in forced_charge else discharge_steps[t] + 1):
            delivered = min(k * step * eta, load)
            cost = (
                price * (load - delivered) + wear * delivered - export_credit * surplus
            )
            for s in range(k, states + 1):
                total = cost + next_value[s - k]
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = -k
                    pvcharged[s] = False

        credit = credits[t]
        sell = [False] * (states + 1)
        for k in range(
            sell_min_steps[t], 1 if t in forced_charge else sell_max_steps[t] + 1
        ):
            out = k * step * eta
            cost = wear * out - credit * (out - load) - export_credit * surplus
            for s in range(k + sell_reserve, states + 1):
                total = cost + next_value[s - k]
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = -k
                    sell[s] = True
                    pvcharged[s] = False

        choices[t] = choice
        sold[t] = sell
        pv_charged[t] = pvcharged
        allowed = [False] * (states + 1)
        for s, k in enumerate(choice):
            if k >= 0 or sell[s]:
                continue
            end_state = s + k
            storage_value = (
                eta * (next_value[end_state] - next_value[end_state + 1]) / step
                if end_state < states
                else 0.0
            )
            allowed[s] = (
                export_credit * SOLAR_EXPORT_CREDIT_FACTOR
                <= storage_value + TIE_EPSILON
            )
        solar_charging[t] = allowed
        next_value = value

    modes: list[str] = []
    end_soc_pct: list[float] = []
    allow_solar_charging: list[bool] = []
    state = start_state
    for choice, sell, pvcharge, allowed in zip(
        choices, sold, pv_charged, solar_charging
    ):
        k = choice[state]
        allow_solar_charging.append(allowed[state])
        if k > 0:
            modes.append(MODE_PVCHARGE if pvcharge[state] else MODE_CHARGE)
        elif k < 0:
            modes.append(MODE_SELL if sell[state] else MODE_DISCHARGE)
        else:
            modes.append(MODE_STANDBY)
        state += k
        end_soc_pct.append(min(low + state * pct_per_state, high))
    return PlanResult(modes, end_soc_pct, next_value[start_state], allow_solar_charging)


def _finite(value: float, default: float) -> float:
    """Return ``value`` as a float, or ``default`` when it is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default
