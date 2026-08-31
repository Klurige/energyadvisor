"""Price-only battery schedule optimization helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import highspy
import numpy as np

from .sensor.chargemodehelpers import find_current_mode

_LOGGER = logging.getLogger(__name__)

DEFAULT_CHARGE_EFFICIENCY = 0.95
DEFAULT_DISCHARGE_EFFICIENCY = 0.95
_EPSILON = 1e-6


@dataclass(slots=True)
class BatteryOptimizationInputs:
    """Inputs required to optimize a battery schedule."""

    rates: Sequence[Mapping[str, Any]]
    reference_time: datetime
    current_soc_pct: float | None
    capacity_kwh: float | None
    max_charge_power_w: float | None
    max_discharge_power_w: float | None
    min_soc_pct: float
    max_soc_pct: float
    horizon_hours: float
    optimization_enabled: bool
    charge_efficiency: float = DEFAULT_CHARGE_EFFICIENCY
    discharge_efficiency: float = DEFAULT_DISCHARGE_EFFICIENCY


@dataclass(slots=True)
class BatteryOptimizationResult:
    """Result returned by the battery optimizer."""

    schedule: list[dict[str, Any]]
    current_mode: str
    reason: str
    optimized: bool
    solver: str | None = None
    objective_value: float | None = None
    current_target_soc_pct: float | None = None


@dataclass(slots=True)
class _HighsSolveResult:
    """Solution returned by the direct HiGHS model."""

    charge_values: list[float]
    discharge_values: list[float]
    sell_values: list[float]
    soc_values: list[float]
    objective_value: float | None


@dataclass(slots=True)
class _Slot:
    """Normalized price slot used by the optimizer."""

    start: datetime
    end: datetime
    duration_hours: float
    cost: float
    credit: float


@dataclass(slots=True)
class _ScheduleEntry:
    """Internal schedule entry representation."""

    from_time: datetime
    mode: str
    target_soc_pct: float | None = None
    cost: float | None = None
    credit: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize the schedule entry for entity attributes."""
        payload: dict[str, Any] = {
            "from": self.from_time.strftime("%Y-%m-%dT%H:%M"),
            "mode": self.mode,
            "target_soc": (
                round(self.target_soc_pct, 1)
                if self.target_soc_pct is not None
                else None
            ),
        }
        if self.cost is not None:
            payload["cost"] = round(self.cost, 3)
        if self.credit is not None:
            payload["credit"] = round(self.credit, 3)
        return payload


def _to_reference_timezone(value: datetime, reference_time: datetime) -> datetime:
    """Convert a datetime to the reference timezone when possible."""
    if value.tzinfo is None:
        if reference_time.tzinfo is None:
            return value
        return value.replace(tzinfo=reference_time.tzinfo)
    if reference_time.tzinfo is None:
        return value
    return value.astimezone(reference_time.tzinfo)


def _coerce_float(value: Any) -> float:
    """Coerce a numeric value without silently masking invalid data."""
    return float(value)


def _slot_from_rate(
    rate: Mapping[str, Any],
    reference_time: datetime,
    horizon_end: datetime,
) -> _Slot | None:
    """Normalize a raw rate entry into a bounded optimization slot."""
    start = rate.get("start")
    end = rate.get("end")
    if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
        return None

    start_local = _to_reference_timezone(start, reference_time)
    end_local = _to_reference_timezone(end, reference_time)

    if end_local <= reference_time:
        return None

    clipped_start = max(start_local, reference_time)
    clipped_end = min(end_local, horizon_end)
    if clipped_end <= clipped_start:
        return None

    duration_hours = (clipped_end - clipped_start).total_seconds() / 3600.0
    if duration_hours <= 0:
        return None

    return _Slot(
        start=clipped_start,
        end=clipped_end,
        duration_hours=duration_hours,
        cost=_coerce_float(rate.get("cost", 0.0)),
        credit=_coerce_float(rate.get("credit", 0.0)),
    )


def _normalize_slots(inputs: BatteryOptimizationInputs) -> list[_Slot]:
    """Build normalized slots from raw price data."""
    horizon_end = inputs.reference_time + timedelta(hours=inputs.horizon_hours)
    slots: list[_Slot] = []
    for rate in sorted(
        inputs.rates, key=lambda item: item.get("start") or inputs.reference_time
    ):
        slot = _slot_from_rate(rate, inputs.reference_time, horizon_end)
        if slot is not None:
            slots.append(slot)
    return slots


def _legacy_default_entry(reference_time: datetime) -> _ScheduleEntry:
    """Build the default legacy schedule entry."""
    midnight = reference_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return _ScheduleEntry(from_time=midnight, mode="maxuse")


def _collapse_schedule(
    slots: Sequence[_Slot],
    slot_modes: Sequence[str],
    slot_targets: Sequence[float | None] | None = None,
) -> list[dict[str, Any]]:
    """Collapse per-slot modes into sequential schedule entries."""
    if not slots:
        return []

    if slot_targets is None:
        slot_targets = [None] * len(slots)

    entries: list[_ScheduleEntry] = []
    index = 0
    while index < len(slots):
        mode = slot_modes[index]
        segment_start = index
        while index + 1 < len(slots) and slot_modes[index + 1] == mode:
            index += 1
        segment_end = index

        target_soc_pct = (
            slot_targets[segment_end]
            if mode in {"charge", "discharge", "sell"}
            else None
        )

        entries.append(
            _ScheduleEntry(
                from_time=slots[segment_start].start,
                mode=mode,
                target_soc_pct=target_soc_pct,
                cost=slots[segment_start].cost,
                credit=slots[segment_start].credit,
            )
        )
        index += 1

    return [entry.as_dict() for entry in entries]


def _legacy_slots_from_rates(
    rates: Sequence[Mapping[str, Any]],
    reference_time: datetime,
) -> list[_Slot]:
    """Normalize legacy slots without clipping the active slot to now."""
    slots: list[_Slot] = []
    for rate in sorted(rates, key=lambda item: item.get("start") or reference_time):
        start = rate.get("start")
        end = rate.get("end")
        if not isinstance(start, datetime):
            continue

        start_local = _to_reference_timezone(start, reference_time)
        if isinstance(end, datetime):
            end_local = _to_reference_timezone(end, reference_time)
            if end_local <= start_local:
                end_local = start_local + timedelta(hours=1)
        else:
            end_local = start_local + timedelta(hours=1)
        if start_local.date() < reference_time.date():
            continue

        duration_hours = (end_local - start_local).total_seconds() / 3600.0
        if duration_hours <= 0:
            continue

        slots.append(
            _Slot(
                start=start_local,
                end=end_local,
                duration_hours=duration_hours,
                cost=_coerce_float(rate.get("cost", 0.0)),
                credit=_coerce_float(rate.get("credit", 0.0)),
            )
        )

    return slots


def _is_legacy_sell_candidate(slot: _Slot) -> bool:
    """Return whether a legacy slot is eligible for sell mode."""
    return slot.start.hour < 10 or slot.start.hour >= 17


def _select_legacy_sell_indexes(slots: Sequence[_Slot]) -> set[int]:
    """Pick the highest-value legacy sell slots per local day."""
    candidates_by_day: dict[str, list[tuple[int, _Slot]]] = {}
    for index, slot in enumerate(slots):
        if _is_legacy_sell_candidate(slot):
            day_key = slot.start.date().isoformat()
            candidates_by_day.setdefault(day_key, []).append((index, slot))

    selected_indexes: set[int] = set()
    for day_key in sorted(candidates_by_day):
        ranked_candidates = sorted(
            candidates_by_day[day_key],
            key=lambda item: (-item[1].credit, item[1].start, item[0]),
        )
        for index, _slot in ranked_candidates[:6]:
            selected_indexes.add(index)

    return selected_indexes


def build_legacy_schedule(
    rates: Sequence[Mapping[str, Any]],
    reference_time: datetime,
) -> list[dict[str, Any]]:
    """Build the current fallback price-only schedule."""
    slots = _legacy_slots_from_rates(rates, reference_time)
    default_entry = _legacy_default_entry(reference_time).as_dict()

    if not slots:
        return [default_entry]

    sell_indexes = _select_legacy_sell_indexes(slots)
    slot_modes = [
        "sell" if index in sell_indexes else "maxuse"
        for index in range(len(slots))
    ]

    schedule = _collapse_schedule(slots, slot_modes)
    if schedule and schedule[0]["from"] != default_entry["from"]:
        schedule.insert(0, default_entry)
    elif not schedule:
        schedule = [default_entry]

    return schedule


def _build_segment_schedule(
    slots: Sequence[_Slot],
    charge_values: Sequence[float],
    discharge_values: Sequence[float],
    sell_values: Sequence[float],
    soc_values: Sequence[float],
    capacity_kwh: float,
    min_soc_kwh: float,
) -> list[dict[str, Any]]:
    """Convert solved slot values into a compact sequential schedule."""
    if not slots:
        return []

    slot_modes: list[str] = []
    slot_targets: list[float | None] = []
    for index, _slot in enumerate(slots):
        charge_energy = charge_values[index]
        discharge_energy = discharge_values[index]
        sell_energy = sell_values[index]
        soc_after = soc_values[index + 1]

        mode, active_energy = max(
            (
                ("charge", charge_energy),
                ("discharge", discharge_energy),
                ("sell", sell_energy),
            ),
            key=lambda item: item[1],
        )
        if active_energy > _EPSILON:
            target_soc_pct = round((soc_after / capacity_kwh) * 100.0, 1)
        else:
            mode = "maxuse" if soc_values[index] > min_soc_kwh + _EPSILON else "standby"
            target_soc_pct = None

        slot_modes.append(mode)
        slot_targets.append(target_soc_pct)

    return _collapse_schedule(slots, slot_modes, slot_targets)


def _resolve_current_target(
    schedule: Sequence[dict[str, Any]],
    current_mode: str,
    reference_time: datetime,
) -> float | None:
    """Return the target SOC for the currently active schedule entry."""
    current_entry = find_current_mode(list(schedule), reference_time)
    if current_entry.get("mode") != current_mode:
        return None
    target_soc = current_entry.get("target_soc")
    if target_soc is None:
        return None
    try:
        return float(target_soc)
    except (TypeError, ValueError):
        return None


def _add_highs_variable(
    highs: highspy.Highs,
    lower_bound: float,
    upper_bound: float,
    integer: bool = False,
) -> int:
    """Add a HiGHS variable and return its column index."""
    column_index = highs.getNumCol()
    status = highs.addVar(float(lower_bound), float(upper_bound))
    if status != highspy.HighsStatus.kOk:
        raise RuntimeError(f"Failed to add HiGHS variable: {status}")
    if integer:
        highs.setInteger(column_index)
    return column_index


def _add_highs_row(
    highs: highspy.Highs,
    lower_bound: float,
    upper_bound: float,
    indices: Sequence[int],
    values: Sequence[float],
) -> None:
    """Add a sparse HiGHS row constraint."""
    status = highs.addRow(
        float(lower_bound),
        float(upper_bound),
        len(indices),
        np.asarray(indices, dtype=np.int32),
        np.asarray(values, dtype=float),
    )
    if status != highspy.HighsStatus.kOk:
        raise RuntimeError(f"Failed to add HiGHS row: {status}")


def _solve_with_highs(
    inputs: BatteryOptimizationInputs,
    slots: Sequence[_Slot],
    charge_limit: Sequence[float],
    discharge_limit: Sequence[float],
    initial_soc_kwh: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
) -> _HighsSolveResult | None:
    """Solve the battery optimization model directly with HiGHS."""
    highs = highspy.Highs()
    highs.setOptionValue("output_flag", False)
    highs.setMinimize()

    infinity = highs.getInfinity()

    charge_idx: list[int] = []
    discharge_idx: list[int] = []
    sell_idx: list[int] = []
    charge_mode_idx: list[int] = []
    discharge_mode_idx: list[int] = []
    sell_mode_idx: list[int] = []
    idle_mode_idx: list[int] = []
    soc_idx: list[int] = []

    for slot_index, _slot in enumerate(slots):
        charge_idx.append(_add_highs_variable(highs, 0.0, charge_limit[slot_index]))
        discharge_idx.append(
            _add_highs_variable(highs, 0.0, discharge_limit[slot_index])
        )
        sell_idx.append(_add_highs_variable(highs, 0.0, discharge_limit[slot_index]))
        charge_mode_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        discharge_mode_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        sell_mode_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        idle_mode_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))

    soc_idx.append(
        _add_highs_variable(highs, initial_soc_kwh, initial_soc_kwh, integer=False)
    )
    for _ in slots:
        soc_idx.append(_add_highs_variable(highs, soc_min_kwh, soc_max_kwh))

    num_cols = highs.getNumCol()
    costs = np.zeros(num_cols, dtype=float)
    for slot_index, slot in enumerate(slots):
        costs[charge_idx[slot_index]] = slot.cost
        costs[discharge_idx[slot_index]] = -slot.cost
        costs[sell_idx[slot_index]] = -slot.credit

    status = highs.changeColsCost(
        num_cols,
        np.arange(num_cols, dtype=np.int32),
        costs,
    )
    if status != highspy.HighsStatus.kOk:
        _LOGGER.warning("Failed to assign HiGHS objective costs: %s", status)
        return None

    for slot_index, slot in enumerate(slots):
        _add_highs_row(
            highs,
            1.0,
            1.0,
            [
                charge_mode_idx[slot_index],
                discharge_mode_idx[slot_index],
                sell_mode_idx[slot_index],
                idle_mode_idx[slot_index],
            ],
            [1.0, 1.0, 1.0, 1.0],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [charge_idx[slot_index], charge_mode_idx[slot_index]],
            [1.0, -charge_limit[slot_index]],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [discharge_idx[slot_index], discharge_mode_idx[slot_index]],
            [1.0, -discharge_limit[slot_index]],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [sell_idx[slot_index], sell_mode_idx[slot_index]],
            [1.0, -discharge_limit[slot_index]],
        )
        _add_highs_row(
            highs,
            0.0,
            0.0,
            [
                soc_idx[slot_index + 1],
                soc_idx[slot_index],
                charge_idx[slot_index],
                discharge_idx[slot_index],
                sell_idx[slot_index],
            ],
            [
                1.0,
                -1.0,
                -inputs.charge_efficiency,
                1.0 / inputs.discharge_efficiency,
                1.0 / inputs.discharge_efficiency,
            ],
        )

    try:
        run_status = highs.run()
    except Exception as exc:  # pragma: no cover - defensive solver guard
        _LOGGER.warning("HiGHS raised an exception while solving: %s", exc)
        return None

    if run_status != highspy.HighsStatus.kOk:
        _LOGGER.warning("HiGHS returned a non-OK run status: %s", run_status)
        return None

    model_status = highs.getModelStatus()
    if model_status != highspy.HighsModelStatus.kOptimal:
        _LOGGER.warning("HiGHS returned status %s", model_status)
        return None

    solution = highs.getSolution()
    if not solution.value_valid:
        _LOGGER.warning("HiGHS returned an invalid primal solution")
        return None

    objective_value: float | None
    try:
        objective_value = float(highs.getObjectiveValue())
    except Exception:  # pragma: no cover - defensive solver guard
        objective_value = None

    return _HighsSolveResult(
        charge_values=[float(solution.col_value[index]) for index in charge_idx],
        discharge_values=[float(solution.col_value[index]) for index in discharge_idx],
        sell_values=[float(solution.col_value[index]) for index in sell_idx],
        soc_values=[float(solution.col_value[index]) for index in soc_idx],
        objective_value=objective_value,
    )


def _fallback_result(inputs: BatteryOptimizationInputs, reason: str) -> BatteryOptimizationResult:
    """Return the legacy schedule when the optimizer cannot run."""
    schedule = build_legacy_schedule(inputs.rates, inputs.reference_time)
    current_mode_entry = find_current_mode(schedule, inputs.reference_time)
    current_mode = str(current_mode_entry.get("mode", "unknown"))
    current_target = _resolve_current_target(
        schedule, current_mode, inputs.reference_time
    )
    return BatteryOptimizationResult(
        schedule=schedule,
        current_mode=current_mode,
        current_target_soc_pct=current_target,
        reason=reason,
        optimized=False,
    )


def optimize_battery_schedule(
    inputs: BatteryOptimizationInputs,
) -> BatteryOptimizationResult:
    """Optimize a battery schedule using price-only linear programming."""
    if not inputs.optimization_enabled:
        return _fallback_result(
            inputs, "Battery optimization is disabled; using legacy price schedule."
        )

    if (
        inputs.capacity_kwh is None
        or inputs.max_charge_power_w is None
        or inputs.max_discharge_power_w is None
    ):
        return _fallback_result(
            inputs,
            "Battery capacity or power limits are missing; using legacy price schedule.",
        )

    current_soc_pct = inputs.current_soc_pct
    if current_soc_pct is None:
        return _fallback_result(
            inputs,
            "Battery SoC sensor is unavailable; using legacy price schedule.",
        )

    if inputs.min_soc_pct >= inputs.max_soc_pct:
        return _fallback_result(
            inputs,
            "Battery SoC bounds are invalid; using legacy price schedule.",
        )

    if inputs.charge_efficiency <= 0 or inputs.discharge_efficiency <= 0:
        return _fallback_result(
            inputs,
            "Battery efficiencies are invalid; using legacy price schedule.",
        )

    slots = _normalize_slots(inputs)
    if not slots:
        return _fallback_result(
            inputs, "No price slots were available; using legacy price schedule."
        )

    soc_min_kwh = inputs.capacity_kwh * (inputs.min_soc_pct / 100.0)
    soc_max_kwh = inputs.capacity_kwh * (inputs.max_soc_pct / 100.0)
    initial_soc_kwh = inputs.capacity_kwh * (current_soc_pct / 100.0)
    initial_soc_kwh = min(max(initial_soc_kwh, soc_min_kwh), soc_max_kwh)

    charge_limit = [
        inputs.max_charge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]
    discharge_limit = [
        inputs.max_discharge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]

    solve_result = _solve_with_highs(
        inputs,
        slots,
        charge_limit,
        discharge_limit,
        initial_soc_kwh,
        soc_min_kwh,
        soc_max_kwh,
    )
    if solve_result is None:
        return _fallback_result(
            inputs, "HiGHS failed to solve the schedule; using legacy price schedule."
        )

    schedule = _build_segment_schedule(
        slots,
        solve_result.charge_values,
        solve_result.discharge_values,
        solve_result.sell_values,
        solve_result.soc_values,
        inputs.capacity_kwh,
        soc_min_kwh,
    )
    current_entry = find_current_mode(schedule, inputs.reference_time)
    current_mode = str(current_entry.get("mode", "unknown"))
    current_target = None
    target_value = current_entry.get("target_soc")
    if target_value is not None:
        try:
            current_target = float(target_value)
        except (TypeError, ValueError):
            current_target = None

    return BatteryOptimizationResult(
        schedule=schedule,
        current_mode=current_mode,
        current_target_soc_pct=current_target,
        reason=(
            f"Optimized {inputs.horizon_hours:g}h price schedule with HiGHS "
            f"from {current_soc_pct:.1f}% SoC."
        ),
        optimized=True,
        solver="HIGHS",
        objective_value=solve_result.objective_value,
    )
