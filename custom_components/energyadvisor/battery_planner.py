"""Battery schedule planner: an exact dynamic programme over state of charge.

The planner has no Home Assistant dependency and is pure Python. It takes a
chronological list of slots (duration, grid price, household load) and decides
for each slot whether the battery should charge from the grid, discharge into
the household or stay idle, so that the total grid cost over the horizon is as
low as possible.

Model:
- The usable energy window is ``capacity * (max_soc - min_soc)``. The state of
  charge is discretised in at most 160 equal energy steps of at least 0.125 kWh
  (the step is stretched slightly so that the top state is exactly ``max_soc``).
- ``charge`` moves ``k`` steps into the battery. The grid delivers the
  household load plus ``k * step / eta_charge``. ``k`` is limited by the charge
  power and the room left in the battery.
- ``discharge`` takes ``k`` steps out of the battery and delivers
  ``min(k * step * eta_discharge, load)`` to the household. The grid delivers
  the rest of the load. The battery never exports. ``k`` is limited by the
  discharge power, the load and the energy left in the battery.
- ``standby`` leaves the battery alone; the grid delivers the load.
- Each kWh the battery delivers adds ``wear_cost_per_kwh``.
- Energy left in the battery at the end of the horizon is worth nothing, so
  callers should re-plan regularly (rolling horizon).
- Ties go to ``standby``, and otherwise to the smaller action.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MODE_STANDBY = "standby"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"

MIN_STEP_KWH = 0.125
MAX_STATES = 160
DEFAULT_ROUND_TRIP_EFFICIENCY = 0.9
# An action must beat standby by more than this to be chosen.
TIE_EPSILON = 1e-9
# Absorbs float noise when rounding power / step ratios down or up.
LIMIT_EPSILON = 1e-9


@dataclass(frozen=True)
class BatteryParams:
    """Battery hardware and cost parameters.

    ``round_trip_efficiency`` is split evenly over charging and discharging
    (``eta = sqrt(round_trip_efficiency)``). ``wear_cost_per_kwh`` is the cost
    of each kWh the battery delivers, in the same currency as the slot prices.
    """

    capacity_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY
    wear_cost_per_kwh: float = 0.0


@dataclass(frozen=True)
class PlanSlot:
    """One planning slot.

    ``import_cost`` is the price per kWh imported from the grid (fees and VAT
    included) and ``load_kwh`` the energy the household uses during the slot.
    """

    duration_h: float
    import_cost: float
    load_kwh: float


@dataclass(frozen=True)
class PlanResult:
    """The plan: one mode and one planned end-of-slot SoC (%) per slot.

    ``total_cost`` is the grid cost plus wear over all slots; energy left in
    the battery at the end counts as zero.
    """

    modes: list[str]
    end_soc_pct: list[float]
    total_cost: float


def plan_battery(
    slots: Sequence[PlanSlot], params: BatteryParams, start_soc_pct: float
) -> PlanResult:
    """Return the cheapest battery plan for ``slots``.

    Inputs:
        - ``slots`` in chronological order.
        - Battery ``params`` and the state of charge at the start of the first
          slot (clamped into the allowed window and snapped to the nearest
          step).
    Outputs:
        - A ``PlanResult`` with one entry per slot. No slots give an empty
          result.
    Bad input is handled as follows: a negative or non-finite load counts as
    zero, a negative or non-finite duration as zero (no battery action), and a
    slot with a non-finite price allows no battery action and adds no cost.
    Negative prices are valid. Non-finite or negative powers count as zero,
    the efficiency is clamped into 0.01..1 and a negative or non-finite wear
    cost counts as zero. When the battery has no usable energy window every
    slot is ``standby`` and the SoC stays where it is.
    """
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
            sum(price * load for price, load in zip(prices, loads)),
        )

    states = min(MAX_STATES, max(1, round(usable_kwh / MIN_STEP_KWH)))
    step = usable_kwh / states
    pct_per_state = (high - low) / states
    eta = math.sqrt(min(max(_finite(params.round_trip_efficiency, 0.9), 0.01), 1.0))
    wear = max(_finite(params.wear_cost_per_kwh, 0.0), 0.0)
    max_charge_kw = max(_finite(params.max_charge_power_kw, 0.0), 0.0)
    max_discharge_kw = max(_finite(params.max_discharge_power_kw, 0.0), 0.0)

    start_soc = min(max(_finite(start_soc_pct, low), low), high)
    start_state = round((start_soc - low) / pct_per_state)

    charge_steps = [
        min(states, math.floor(max_charge_kw * hours / step + LIMIT_EPSILON))
        for hours in durations
    ]
    discharge_steps = [
        min(
            states,
            math.floor(max_discharge_kw * hours / step + LIMIT_EPSILON),
            math.ceil(load / (step * eta) - LIMIT_EPSILON),
        )
        for hours, load in zip(durations, loads)
    ]

    # choices[t][s]: steps charged (> 0) or discharged (< 0) in slot t at state s.
    choices: list[list[int]] = [[]] * count
    next_value = [0.0] * (states + 1)
    for t in range(count - 1, -1, -1):
        price = prices[t]
        load = loads[t]
        value = [price * load + v for v in next_value]
        choice = [0] * (states + 1)

        for k in range(1, charge_steps[t] + 1):
            cost = price * (load + k * step / eta)
            for s, future in enumerate(next_value[k:]):
                total = cost + future
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = k

        for k in range(1, discharge_steps[t] + 1):
            delivered = min(k * step * eta, load)
            cost = price * (load - delivered) + wear * delivered
            for s in range(k, states + 1):
                total = cost + next_value[s - k]
                if total + TIE_EPSILON < value[s]:
                    value[s] = total
                    choice[s] = -k

        choices[t] = choice
        next_value = value

    modes: list[str] = []
    end_soc_pct: list[float] = []
    state = start_state
    for choice in choices:
        k = choice[state]
        state += k
        modes.append(
            MODE_CHARGE if k > 0 else MODE_DISCHARGE if k < 0 else MODE_STANDBY
        )
        end_soc_pct.append(min(low + state * pct_per_state, high))
    return PlanResult(modes, end_soc_pct, next_value[start_state])


def _finite(value: float, default: float) -> float:
    """Return ``value`` as a float, or ``default`` when it is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default
