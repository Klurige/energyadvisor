"""Price-, solar- and load-aware battery schedule MILP optimizer.

This module implements the battery charge mode optimizer specified in
``docs/battery_charge_mode_optimiser.md``. It solves a HiGHS mixed-integer
linear program over a 15-minute-resolution horizon, with mutually exclusive
battery modes (``standby``, ``maxuse``, ``charge``, ``discharge``, ``sell``),
explicit PV/grid energy-balance equations, a simultaneous import/export
prohibition, a solar-headroom soft penalty and a terminal SoC reserve policy.
The optimizer degrades to a deterministic ``maxuse`` fallback schedule
whenever configuration, SoC, price or solver data is invalid or unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import highspy
import numpy as np

from .sensor.chargemodehelpers import find_current_mode

_LOGGER = logging.getLogger(__name__)

DEFAULT_CHARGE_EFFICIENCY = 0.95
DEFAULT_DISCHARGE_EFFICIENCY = 0.95
DEFAULT_DEGRADATION_COST = 0.0
_EPSILON = 1e-6
#: Minimum battery export in a ``sell`` slot, in kWh. Keeps ``sell`` and
#: ``discharge`` distinguishable: a slot labelled ``sell`` must export.
_MIN_SELL_EXPORT_KWH = 1e-3
#: Pass-3 tie-break weight discouraging a `sell` label when an identical-cost
#: `discharge` labelling exists. Kept well below the flow-minimization weight.
_SELL_LABEL_TIE_BREAK_WEIGHT = 1e-3

#: Battery modes in mutual-exclusivity order. ``maxuse`` and ``standby`` are
#: both idle modes; ``maxuse`` is the preferred fallback/tie-break default.
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"
MODE_SELL = "sell"
MODE_MAXUSE = "maxuse"
MODE_STANDBY = "standby"
_MODE_ORDER = (MODE_CHARGE, MODE_DISCHARGE, MODE_SELL, MODE_MAXUSE, MODE_STANDBY)
#: Modes whose schedule entry must carry a numeric end-of-slot target_soc.
_TARGET_SOC_MODES = frozenset({MODE_CHARGE, MODE_SELL})


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
    solar_forecasts: Sequence[Mapping[str, Any]] | None = None
    load_forecasts: Sequence[Mapping[str, Any]] | None = None
    charge_efficiency: float = DEFAULT_CHARGE_EFFICIENCY
    discharge_efficiency: float = DEFAULT_DISCHARGE_EFFICIENCY
    degradation_cost: float = DEFAULT_DEGRADATION_COST


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
class _Slot:
    """Normalized price slot used as the canonical optimization time grid."""

    start: datetime
    end: datetime
    duration_hours: float
    cost: float
    credit: float


@dataclass(slots=True)
class _SolarSlot:
    """Normalized solar forecast slot used by the optimizer."""

    start: datetime
    end: datetime
    duration_hours: float
    power_kw: float


@dataclass(slots=True)
class _LoadSlot:
    """Normalized household load forecast slot used by the optimizer."""

    start: datetime
    end: datetime
    duration_hours: float
    energy_kwh: float


@dataclass(slots=True)
class _MilpResult:
    """Solution returned by the direct HiGHS MILP model."""

    mode_indexes: list[int]
    soc_values: list[float]
    grid_import_values: list[float]
    grid_export_values: list[float]
    charge_grid_values: list[float]
    charge_pv_values: list[float]
    discharge_load_values: list[float]
    discharge_export_values: list[float]
    objective_value: float | None


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


def _coerce_datetime(value: Any, reference_time: datetime) -> datetime | None:
    """Coerce a datetime-like value and align it with the reference timezone."""
    if isinstance(value, datetime):
        return _to_reference_timezone(value, reference_time)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _to_reference_timezone(parsed, reference_time)
    return None


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
    """Build normalized price slots from raw rate data (the canonical grid)."""
    horizon_end = inputs.reference_time + timedelta(hours=inputs.horizon_hours)
    slots: list[_Slot] = []
    for rate in sorted(
        inputs.rates, key=lambda item: item.get("start") or inputs.reference_time
    ):
        slot = _slot_from_rate(rate, inputs.reference_time, horizon_end)
        if slot is not None:
            slots.append(slot)
    return slots


def _solar_slot_from_forecast(
    forecast: Mapping[str, Any],
    reference_time: datetime,
    horizon_end: datetime,
) -> _SolarSlot | None:
    """Normalize a raw solar forecast entry into a bounded solar slot."""
    start = _coerce_datetime(
        forecast.get("start") or forecast.get("period_start"), reference_time
    )
    end = _coerce_datetime(
        forecast.get("end") or forecast.get("period_end") or forecast.get("datetime"),
        reference_time,
    )

    if start is None and end is None:
        return None
    if start is None and end is not None:
        start = end - timedelta(minutes=15)
    if end is None and start is not None:
        end = start + timedelta(minutes=15)
    if start is None or end is None:
        return None
    if end <= start:
        end = start + timedelta(minutes=15)

    clipped_start = max(start, reference_time)
    clipped_end = min(end, horizon_end)
    if clipped_end <= clipped_start:
        return None

    power_kw_value = (
        forecast.get("pow")
        if forecast.get("pow") is not None
        else forecast.get("power_kw")
    )
    if power_kw_value is None:
        power_kw_value = forecast.get("pv_estimate")
    if power_kw_value is None:
        power_kw_value = forecast.get("raw", 0.0)

    try:
        power_kw = max(0.0, float(power_kw_value))
    except (TypeError, ValueError):
        return None

    duration_hours = (clipped_end - clipped_start).total_seconds() / 3600.0
    if duration_hours <= 0:
        return None

    return _SolarSlot(
        start=clipped_start,
        end=clipped_end,
        duration_hours=duration_hours,
        power_kw=power_kw,
    )


def _normalize_solar_slots(inputs: BatteryOptimizationInputs) -> list[_SolarSlot]:
    """Build normalized solar forecast slots from raw forecast data."""
    solar_forecasts = inputs.solar_forecasts or ()
    if not solar_forecasts:
        return []

    horizon_end = inputs.reference_time + timedelta(hours=inputs.horizon_hours)
    slots: list[_SolarSlot] = []
    for forecast in sorted(
        solar_forecasts,
        key=lambda item: _coerce_datetime(
            item.get("start")
            or item.get("period_start")
            or item.get("end")
            or item.get("period_end")
            or item.get("datetime"),
            inputs.reference_time,
        )
        or inputs.reference_time,
    ):
        slot = _solar_slot_from_forecast(forecast, inputs.reference_time, horizon_end)
        if slot is not None and slot.power_kw > 0:
            slots.append(slot)
    return slots


def _load_slot_from_forecast(
    forecast: Mapping[str, Any],
    reference_time: datetime,
    horizon_end: datetime,
) -> _LoadSlot | None:
    """Normalize a raw load forecast entry (start/end/load_kwh) into a slot."""
    start = _coerce_datetime(forecast.get("start"), reference_time)
    end = _coerce_datetime(forecast.get("end"), reference_time)
    if start is None or end is None or end <= start:
        return None

    clipped_start = max(start, reference_time)
    clipped_end = min(end, horizon_end)
    if clipped_end <= clipped_start:
        return None

    try:
        load_kwh = float(forecast.get("load_kwh", 0.0))
    except (TypeError, ValueError):
        return None

    original_duration_hours = (end - start).total_seconds() / 3600.0
    if original_duration_hours <= 0:
        return None

    overlap_hours = (clipped_end - clipped_start).total_seconds() / 3600.0
    energy_kwh = load_kwh * (overlap_hours / original_duration_hours)

    return _LoadSlot(
        start=clipped_start,
        end=clipped_end,
        duration_hours=overlap_hours,
        energy_kwh=energy_kwh,
    )


def _normalize_load_slots(inputs: BatteryOptimizationInputs) -> list[_LoadSlot]:
    """Build normalized household load forecast slots."""
    load_forecasts = inputs.load_forecasts or ()
    if not load_forecasts:
        return []

    horizon_end = inputs.reference_time + timedelta(hours=inputs.horizon_hours)
    slots: list[_LoadSlot] = []
    for forecast in sorted(
        load_forecasts,
        key=lambda item: _coerce_datetime(item.get("start"), inputs.reference_time)
        or inputs.reference_time,
    ):
        slot = _load_slot_from_forecast(forecast, inputs.reference_time, horizon_end)
        if slot is not None:
            slots.append(slot)
    return slots


def household_forecast_slots_to_load_forecasts(
    forecast_slots: Sequence[Mapping[str, Any]],
    reference_time: datetime,
) -> list[dict[str, Any]]:
    """Adapt ``HouseholdForecastCoordinator.forecast_slots`` to the optimizer shape.

    Reads the coordinator's actual keys ``slot["from"]`` (local slot start,
    ``YYYY-MM-DDTHH:MM``) and ``slot["load"]`` (kW), derives
    ``end = from + 15 minutes`` and returns
    ``{"start": ..., "end": ..., "load_kwh": ...}`` entries. The kWh
    conversion happens later, per-overlap, in ``_load_slot_from_forecast``;
    here we simply express the raw 15-minute slot's total energy so a full
    slot maps to ``load_kw * 0.25``.
    """
    load_forecasts: list[dict[str, Any]] = []
    for slot in forecast_slots:
        from_value = slot.get("from")
        load_value = slot.get("load")
        if from_value is None or load_value is None:
            continue
        start = _coerce_datetime(from_value, reference_time)
        if start is None:
            continue
        end = start + timedelta(minutes=15)
        try:
            load_kw = float(load_value)
        except (TypeError, ValueError):
            continue
        load_forecasts.append(
            {
                "start": start,
                "end": end,
                "load_kwh": load_kw * 0.25,
            }
        )
    return load_forecasts


def _energy_by_price_slot_from_power(
    slots: Sequence[_Slot], source_slots: Sequence[_SolarSlot]
) -> list[float]:
    """Map power-based forecast energy (e.g. PV) onto each price slot."""
    if not slots or not source_slots:
        return [0.0 for _ in slots]

    energies: list[float] = []
    for slot in slots:
        total_kwh = 0.0
        for source_slot in source_slots:
            overlap_start = max(slot.start, source_slot.start)
            overlap_end = min(slot.end, source_slot.end)
            if overlap_end <= overlap_start:
                continue
            overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600.0
            total_kwh += source_slot.power_kw * overlap_hours
        energies.append(total_kwh)
    return energies


def _energy_by_price_slot_from_energy(
    slots: Sequence[_Slot], source_slots: Sequence[_LoadSlot]
) -> list[float]:
    """Apportion energy-based forecast entries (e.g. load) onto price slots."""
    if not slots or not source_slots:
        return [0.0 for _ in slots]

    energies: list[float] = []
    for slot in slots:
        total_kwh = 0.0
        for source_slot in source_slots:
            overlap_start = max(slot.start, source_slot.start)
            overlap_end = min(slot.end, source_slot.end)
            if overlap_end <= overlap_start:
                continue
            overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600.0
            source_duration = source_slot.duration_hours
            if source_duration <= 0:
                continue
            total_kwh += source_slot.energy_kwh * (overlap_hours / source_duration)
        energies.append(total_kwh)
    return energies


def _high_solar_window(pv_t: Sequence[float]) -> tuple[int, int, float]:
    """Return ``(t_hi, t_end, pv_hi)`` describing the high-solar window."""
    total = len(pv_t)
    max_pv = max(pv_t) if pv_t else 0.0
    if max_pv <= 0:
        return total, total, 0.0

    pv_hi = 0.75 * max_pv
    t_hi = next((t for t, value in enumerate(pv_t) if value >= pv_hi), total)
    if t_hi >= total:
        return total, total, pv_hi

    t_end = t_hi
    while t_end < total and pv_t[t_end] >= pv_hi:
        t_end += 1

    return t_hi, t_end, pv_hi


def _headroom_profile(
    pv_t: Sequence[float],
    load_t: Sequence[float],
    soc_min_kwh: float,
    soc_max_kwh: float,
) -> tuple[list[float], int]:
    """Build the pre-solar-window headroom_t profile and the window start t_hi."""
    total = len(pv_t)
    t_hi, t_end, _pv_hi = _high_solar_window(pv_t)
    headroom = [0.0] * total
    if t_hi >= total:
        return headroom, t_hi

    max_headroom_kwh = max(0.0, soc_max_kwh - soc_min_kwh)
    for t in range(t_hi):
        surplus = sum(pv_t[k] - load_t[k] for k in range(t, t_end))
        surplus_pre_hi = max(0.0, surplus)
        headroom[t] = min(max_headroom_kwh, surplus_pre_hi)

    return headroom, t_hi


def _terminal_value_per_kwh(
    last_price: float, last_credit: float, discharge_efficiency: float
) -> float:
    """Value assigned to one kWh of SoC remaining at the horizon end."""
    return max(last_price, last_credit, 0.0) * discharge_efficiency


def _headroom_penalty_factor(last_price: float, last_credit: float) -> float:
    """Penalty coefficient applied to headroom shortfall in the objective."""
    return max(last_price, last_credit, 1.0)


def _terminal_reserve_kwh(
    capacity_kwh: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
    soc_0_kwh: float,
    charge_efficiency: float,
    max_charge_kwh: Sequence[float],
) -> float:
    """Compute the capped terminal SoC reserve required at the horizon end."""
    usable_range = max(0.0, soc_max_kwh - soc_min_kwh)
    reachable = soc_0_kwh - soc_min_kwh + charge_efficiency * sum(max_charge_kwh)
    return max(
        0.0,
        min(
            usable_range,
            max(0.10 * capacity_kwh, 0.05 * usable_range),
            reachable,
        ),
    )


def _default_entry(reference_time: datetime) -> _ScheduleEntry:
    """Build the default single fallback schedule entry."""
    midnight = reference_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return _ScheduleEntry(from_time=midnight, mode=MODE_MAXUSE)


def _build_fallback_schedule(
    rates: Sequence[Mapping[str, Any]],
    reference_time: datetime,
) -> list[dict[str, Any]]:
    """Build the canonical maxuse fallback schedule.

    The fallback is strictly algorithmic: every known price slot is emitted
    in ``maxuse`` mode. No legacy sell heuristic is used, matching the
    fallback/failure policy in the optimizer plan.
    """
    default_entry = _default_entry(reference_time).as_dict()

    slots: list[_Slot] = []
    horizon_end = reference_time + timedelta(hours=48.0)
    for rate in sorted(rates, key=lambda item: item.get("start") or reference_time):
        start = rate.get("start")
        if not isinstance(start, datetime):
            continue
        start_local = _to_reference_timezone(start, reference_time)
        end = rate.get("end")
        if isinstance(end, datetime):
            end_local = _to_reference_timezone(end, reference_time)
            if end_local <= start_local:
                end_local = start_local + timedelta(hours=1)
        else:
            end_local = start_local + timedelta(hours=1)
        if end_local <= reference_time or start_local >= horizon_end:
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

    if not slots:
        return [default_entry]

    entries = [
        _ScheduleEntry(
            from_time=slot.start,
            mode=MODE_MAXUSE,
            target_soc_pct=None,
            cost=slot.cost,
            credit=slot.credit,
        )
        for slot in slots
    ]
    return [entry.as_dict() for entry in entries]


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


def _solve_milp(
    inputs: BatteryOptimizationInputs,
    slots: Sequence[_Slot],
    load_t: Sequence[float],
    pv_t: Sequence[float],
    max_charge_kwh: Sequence[float],
    max_discharge_kwh: Sequence[float],
    initial_soc_kwh: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
    headroom_t: Sequence[float],
    t_hi: int,
    terminal_value: float,
    penalty_factor: float,
    reserve_kwh: float,
) -> _MilpResult | None:
    """Solve the MILP, degrading to ``None`` (solver failure) on any error."""
    try:
        return _solve_milp_unsafe(
            inputs,
            slots,
            load_t,
            pv_t,
            max_charge_kwh,
            max_discharge_kwh,
            initial_soc_kwh,
            soc_min_kwh,
            soc_max_kwh,
            headroom_t,
            t_hi,
            terminal_value,
            penalty_factor,
            reserve_kwh,
        )
    except Exception as exc:  # pragma: no cover - defensive solver guard
        _LOGGER.warning("HiGHS failed to build/solve the battery MILP: %s", exc)
        return None


def _solve_milp_unsafe(
    inputs: BatteryOptimizationInputs,
    slots: Sequence[_Slot],
    load_t: Sequence[float],
    pv_t: Sequence[float],
    max_charge_kwh: Sequence[float],
    max_discharge_kwh: Sequence[float],
    initial_soc_kwh: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
    headroom_t: Sequence[float],
    t_hi: int,
    terminal_value: float,
    penalty_factor: float,
    reserve_kwh: float,
) -> _MilpResult | None:
    """Build and solve the full battery MILP with a lexicographic tie-break."""
    total = len(slots)
    if total == 0:
        return None

    highs = highspy.Highs()
    highs.setOptionValue("output_flag", False)
    highs.setMinimize()
    infinity = highs.getInfinity()

    max_import_kwh = [
        load_t[t] + max_charge_kwh[t] + max_charge_kwh[t] + _EPSILON
        for t in range(total)
    ]
    max_export_kwh = [pv_t[t] + max_discharge_kwh[t] + _EPSILON for t in range(total)]

    g_imp_idx: list[int] = []
    g_exp_idx: list[int] = []
    ch_grid_idx: list[int] = []
    ch_pv_idx: list[int] = []
    dis_load_idx: list[int] = []
    dis_export_idx: list[int] = []
    pv_to_load_idx: list[int] = []
    pv_to_export_idx: list[int] = []
    d_grid_idx: list[int] = []
    m_charge_idx: list[int] = []
    m_discharge_idx: list[int] = []
    m_sell_idx: list[int] = []
    m_maxuse_idx: list[int] = []
    m_standby_idx: list[int] = []
    shortfall_idx: dict[int, int] = {}

    for t in range(total):
        g_imp_idx.append(_add_highs_variable(highs, 0.0, max_import_kwh[t]))
        g_exp_idx.append(_add_highs_variable(highs, 0.0, max_export_kwh[t]))
        ch_grid_idx.append(_add_highs_variable(highs, 0.0, max_charge_kwh[t]))
        ch_pv_idx.append(
            _add_highs_variable(highs, 0.0, min(max_charge_kwh[t], pv_t[t]))
        )
        dis_load_idx.append(
            _add_highs_variable(highs, 0.0, min(max_discharge_kwh[t], load_t[t]))
        )
        dis_export_idx.append(_add_highs_variable(highs, 0.0, max_discharge_kwh[t]))
        pv_to_load_idx.append(_add_highs_variable(highs, 0.0, min(pv_t[t], load_t[t])))
        pv_to_export_idx.append(_add_highs_variable(highs, 0.0, pv_t[t]))
        d_grid_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        m_charge_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        m_discharge_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        m_sell_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        m_maxuse_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        m_standby_idx.append(_add_highs_variable(highs, 0.0, 1.0, integer=True))
        if t < t_hi:
            shortfall_idx[t] = _add_highs_variable(highs, 0.0, infinity)

    soc_idx: list[int] = [_add_highs_variable(highs, initial_soc_kwh, initial_soc_kwh)]
    for _ in range(total):
        soc_idx.append(_add_highs_variable(highs, soc_min_kwh, soc_max_kwh))

    eta_ch = inputs.charge_efficiency
    eta_dis = inputs.discharge_efficiency

    for t in range(total):
        # Mutually exclusive battery modes.
        _add_highs_row(
            highs,
            1.0,
            1.0,
            [
                m_charge_idx[t],
                m_discharge_idx[t],
                m_sell_idx[t],
                m_maxuse_idx[t],
                m_standby_idx[t],
            ],
            [1.0, 1.0, 1.0, 1.0, 1.0],
        )
        # Grid-funded charging is only allowed while deliberately in charge
        # mode. PV-funded charging is also allowed in maxuse mode, which
        # represents self-consumption/"maximal use of own production":
        # surplus solar tops up the battery even when there is no active,
        # price-driven charge decision (i.e. no grid import is planned).
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [ch_grid_idx[t], m_charge_idx[t]],
            [1.0, -max_charge_kwh[t]],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [ch_pv_idx[t], m_charge_idx[t], m_maxuse_idx[t]],
            [1.0, -max_charge_kwh[t], -max_charge_kwh[t]],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [ch_grid_idx[t], ch_pv_idx[t], m_charge_idx[t], m_maxuse_idx[t]],
            [1.0, 1.0, -max_charge_kwh[t], -max_charge_kwh[t]],
        )
        # Load-serving discharge is allowed in both discharge and sell mode.
        # A sell slot serves the household from the battery first and exports
        # only the surplus; forcing it to import the load at the same time
        # would collide with the simultaneous import/export prohibition and
        # make sell structurally infeasible whenever load > 0 and PV = 0.
        # Export-only discharge remains reserved for sell mode, which keeps
        # discharge and sell mathematically distinguishable.
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [dis_load_idx[t], m_discharge_idx[t], m_sell_idx[t]],
            [1.0, -max_discharge_kwh[t], -max_discharge_kwh[t]],
        )
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [dis_export_idx[t], m_sell_idx[t]],
            [1.0, -max_discharge_kwh[t]],
        )
        # Total discharge power cap across both discharge sinks.
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [
                dis_load_idx[t],
                dis_export_idx[t],
                m_discharge_idx[t],
                m_sell_idx[t],
            ],
            [1.0, 1.0, -max_discharge_kwh[t], -max_discharge_kwh[t]],
        )
        # A sell slot must actually export from the battery, otherwise it is
        # indistinguishable from a plain discharge slot.
        _add_highs_row(
            highs,
            0.0,
            infinity,
            [dis_export_idx[t], m_sell_idx[t]],
            [1.0, -_MIN_SELL_EXPORT_KWH],
        )
        _add_highs_row(
            highs, -infinity, 0.0, [dis_export_idx[t], g_exp_idx[t]], [1.0, -1.0]
        )
        # SoC balance.
        _add_highs_row(
            highs,
            0.0,
            0.0,
            [
                soc_idx[t + 1],
                soc_idx[t],
                ch_grid_idx[t],
                ch_pv_idx[t],
                dis_load_idx[t],
                dis_export_idx[t],
            ],
            [1.0, -1.0, -eta_ch, -eta_ch, 1.0 / eta_dis, 1.0 / eta_dis],
        )
        # PV allocation: pv_to_load + pv_to_export + b_ch_pv == pv_t.
        _add_highs_row(
            highs,
            pv_t[t],
            pv_t[t],
            [pv_to_load_idx[t], pv_to_export_idx[t], ch_pv_idx[t]],
            [1.0, 1.0, 1.0],
        )
        # Grid balance: g_imp + b_dis_load + pv_to_load == load_t + b_ch_grid.
        _add_highs_row(
            highs,
            load_t[t],
            load_t[t],
            [g_imp_idx[t], dis_load_idx[t], pv_to_load_idx[t], ch_grid_idx[t]],
            [1.0, 1.0, 1.0, -1.0],
        )
        # Export balance: g_exp == pv_to_export + b_dis_export.
        _add_highs_row(
            highs,
            0.0,
            0.0,
            [g_exp_idx[t], pv_to_export_idx[t], dis_export_idx[t]],
            [1.0, -1.0, -1.0],
        )
        # Simultaneous import/export prohibition.
        _add_highs_row(
            highs,
            -infinity,
            0.0,
            [g_imp_idx[t], d_grid_idx[t]],
            [1.0, -max_import_kwh[t]],
        )
        _add_highs_row(
            highs,
            -infinity,
            max_export_kwh[t],
            [g_exp_idx[t], d_grid_idx[t]],
            [1.0, max_export_kwh[t]],
        )
        # Solar headroom shortfall (soft penalty, pre-solar-window slots only).
        # headroom_t is evaluated against the end-of-slot-t SoC (soc_idx[t+1]),
        # i.e. the level reached right after this slot's charge/discharge
        # decision, which is what must leave room for the upcoming PV window.
        if t in shortfall_idx:
            _add_highs_row(
                highs,
                -(soc_max_kwh - headroom_t[t]),
                infinity,
                [shortfall_idx[t], soc_idx[t + 1]],
                [1.0, -1.0],
            )

    # Terminal reserve floor.
    _add_highs_row(
        highs,
        soc_min_kwh + reserve_kwh,
        infinity,
        [soc_idx[total]],
        [1.0],
    )

    num_cols = highs.getNumCol()

    def _set_costs(costs: np.ndarray) -> bool:
        status = highs.changeColsCost(
            num_cols, np.arange(num_cols, dtype=np.int32), costs
        )
        return status == highspy.HighsStatus.kOk

    def _run() -> bool:
        try:
            run_status = highs.run()
        except Exception as exc:  # pragma: no cover - defensive solver guard
            _LOGGER.warning("HiGHS raised an exception while solving: %s", exc)
            return False
        if run_status != highspy.HighsStatus.kOk:
            _LOGGER.warning("HiGHS returned a non-OK run status: %s", run_status)
            return False
        model_status = highs.getModelStatus()
        if model_status != highspy.HighsModelStatus.kOptimal:
            _LOGGER.warning("HiGHS returned status %s", model_status)
            return False
        return True

    # Pass 1: minimize the full economic objective.
    costs = np.zeros(num_cols, dtype=float)
    for t in range(total):
        costs[g_imp_idx[t]] = slots[t].cost
        costs[g_exp_idx[t]] = -slots[t].credit
        degradation = inputs.degradation_cost
        costs[ch_grid_idx[t]] += degradation
        costs[ch_pv_idx[t]] += degradation
        costs[dis_load_idx[t]] += degradation
        costs[dis_export_idx[t]] += degradation
        if t in shortfall_idx:
            costs[shortfall_idx[t]] = penalty_factor
    costs[soc_idx[total]] += -terminal_value

    if not _set_costs(costs):
        return None
    if not _run():
        return None
    solution = highs.getSolution()
    if not solution.value_valid:
        _LOGGER.warning("HiGHS returned an invalid primal solution")
        return None
    try:
        obj_1 = float(highs.getObjectiveValue())
    except Exception:  # pragma: no cover - defensive solver guard
        return None

    nonzero_indices = np.nonzero(costs)[0]
    _add_highs_row(
        highs,
        -infinity,
        obj_1 + 1e-6,
        nonzero_indices.tolist(),
        costs[nonzero_indices].tolist(),
    )

    # Pass 2: maximize the number of maxuse slots (minimize its negative).
    costs2 = np.zeros(num_cols, dtype=float)
    for t in range(total):
        costs2[m_maxuse_idx[t]] = -1.0
    if not _set_costs(costs2):
        return None
    if not _run():
        return None
    solution = highs.getSolution()
    if not solution.value_valid:
        return None
    maxuse_2 = sum(float(solution.col_value[m_maxuse_idx[t]]) for t in range(total))

    _add_highs_row(
        highs,
        maxuse_2 - 1e-6,
        infinity,
        [m_maxuse_idx[t] for t in range(total)],
        [1.0] * total,
    )

    # Pass 3: minimize total absolute battery flow, with a subordinate
    # preference against `sell`. Because `sell` may now also serve load, a
    # marginal slot can be labelled either `sell` or `discharge` at an
    # identical objective; the tiny weight resolves that ambiguity towards
    # the simpler `discharge` label without ever overriding flow
    # minimization or the pinned economic objective.
    costs3 = np.zeros(num_cols, dtype=float)
    for t in range(total):
        costs3[ch_grid_idx[t]] = 1.0
        costs3[ch_pv_idx[t]] = 1.0
        costs3[dis_load_idx[t]] = 1.0
        costs3[dis_export_idx[t]] = 1.0
        costs3[m_sell_idx[t]] = _SELL_LABEL_TIE_BREAK_WEIGHT
    if not _set_costs(costs3):
        return None
    if not _run():
        return None

    solution = highs.getSolution()
    if not solution.value_valid:
        _LOGGER.warning("HiGHS returned an invalid primal solution")
        return None

    mode_indexes: list[int] = []
    for t in range(total):
        mode_values = [
            float(solution.col_value[m_charge_idx[t]]),
            float(solution.col_value[m_discharge_idx[t]]),
            float(solution.col_value[m_sell_idx[t]]),
            float(solution.col_value[m_maxuse_idx[t]]),
            float(solution.col_value[m_standby_idx[t]]),
        ]
        mode_indexes.append(max(range(5), key=lambda index: mode_values[index]))

    # Report the true economic objective from pass 1, not the pass 3
    # tie-break objective (which minimizes total battery flow and is not a
    # meaningful cost figure).
    objective_value = obj_1

    return _MilpResult(
        mode_indexes=mode_indexes,
        soc_values=[float(solution.col_value[index]) for index in soc_idx],
        grid_import_values=[float(solution.col_value[index]) for index in g_imp_idx],
        grid_export_values=[float(solution.col_value[index]) for index in g_exp_idx],
        charge_grid_values=[float(solution.col_value[index]) for index in ch_grid_idx],
        charge_pv_values=[float(solution.col_value[index]) for index in ch_pv_idx],
        discharge_load_values=[
            float(solution.col_value[index]) for index in dis_load_idx
        ],
        discharge_export_values=[
            float(solution.col_value[index]) for index in dis_export_idx
        ],
        objective_value=objective_value,
    )


def _build_schedule_from_milp(
    slots: Sequence[_Slot],
    milp_result: _MilpResult,
    capacity_kwh: float,
    min_soc_pct: float,
    max_soc_pct: float,
) -> list[dict[str, Any]]:
    """Convert the solved MILP result into the public schedule contract."""
    entries: list[_ScheduleEntry] = []
    for index, slot in enumerate(slots):
        mode = _MODE_ORDER[milp_result.mode_indexes[index]]
        target_soc_pct: float | None = None
        if mode in _TARGET_SOC_MODES:
            soc_after = milp_result.soc_values[index + 1]
            target_soc_pct = min(
                max_soc_pct, max(min_soc_pct, (soc_after / capacity_kwh) * 100.0)
            )
        entries.append(
            _ScheduleEntry(
                from_time=slot.start,
                mode=mode,
                target_soc_pct=target_soc_pct,
                cost=slot.cost,
                credit=slot.credit,
            )
        )
    return [entry.as_dict() for entry in entries]


def _fallback_result(
    inputs: BatteryOptimizationInputs, reason: str
) -> BatteryOptimizationResult:
    """Return the canonical maxuse schedule when the optimizer cannot run."""
    schedule = _build_fallback_schedule(inputs.rates, inputs.reference_time)
    current_mode_entry = find_current_mode(schedule, inputs.reference_time)
    current_mode = str(current_mode_entry.get("mode", MODE_MAXUSE))
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
    """Optimize a battery schedule using the full price/PV/load MILP."""
    if not inputs.optimization_enabled:
        return _fallback_result(
            inputs, "Battery optimization is disabled; using maxuse fallback."
        )

    if (
        inputs.capacity_kwh is None
        or inputs.max_charge_power_w is None
        or inputs.max_discharge_power_w is None
    ):
        return _fallback_result(
            inputs,
            "Battery capacity or power limits are missing; using maxuse fallback.",
        )

    current_soc_pct = inputs.current_soc_pct
    if current_soc_pct is None:
        return _fallback_result(
            inputs,
            "Battery SoC sensor is unavailable; using maxuse fallback.",
        )

    if inputs.min_soc_pct >= inputs.max_soc_pct:
        return _fallback_result(
            inputs,
            "Battery SoC bounds are invalid; using maxuse fallback.",
        )

    if inputs.charge_efficiency <= 0 or inputs.discharge_efficiency <= 0:
        return _fallback_result(
            inputs,
            "Battery efficiencies are invalid; using maxuse fallback.",
        )

    horizon_hours = min(max(inputs.horizon_hours, 1.0), 48.0)
    inputs = replace(inputs, horizon_hours=horizon_hours)
    slots = _normalize_slots(inputs)
    if not slots:
        return _fallback_result(
            inputs, "Price data is unavailable; using maxuse fallback."
        )

    solar_slots = _normalize_solar_slots(inputs)
    load_slots = _normalize_load_slots(inputs)

    pv_t = _energy_by_price_slot_from_power(slots, solar_slots)
    load_t = _energy_by_price_slot_from_energy(slots, load_slots)
    load_reason = ""
    if not load_slots:
        load_reason = " (no household load forecast available; assuming zero load)"

    soc_min_kwh = inputs.capacity_kwh * (inputs.min_soc_pct / 100.0)
    soc_max_kwh = inputs.capacity_kwh * (inputs.max_soc_pct / 100.0)
    initial_soc_kwh = inputs.capacity_kwh * (current_soc_pct / 100.0)
    clamp_reason = ""
    if initial_soc_kwh < soc_min_kwh or initial_soc_kwh > soc_max_kwh:
        clamp_reason = " (measured SoC was clamped to configured bounds)"
    initial_soc_kwh = min(max(initial_soc_kwh, soc_min_kwh), soc_max_kwh)

    max_charge_kwh = [
        inputs.max_charge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]
    max_discharge_kwh = [
        inputs.max_discharge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]

    headroom_t, t_hi = _headroom_profile(pv_t, load_t, soc_min_kwh, soc_max_kwh)
    has_headroom = t_hi < len(slots)

    last_price = slots[-1].cost
    last_credit = slots[-1].credit
    terminal_value = _terminal_value_per_kwh(
        last_price, last_credit, inputs.discharge_efficiency
    )
    penalty_factor = _headroom_penalty_factor(last_price, last_credit)
    reserve_kwh = _terminal_reserve_kwh(
        inputs.capacity_kwh,
        soc_min_kwh,
        soc_max_kwh,
        initial_soc_kwh,
        inputs.charge_efficiency,
        max_charge_kwh,
    )

    milp_result = _solve_milp(
        inputs,
        slots,
        load_t,
        pv_t,
        max_charge_kwh,
        max_discharge_kwh,
        initial_soc_kwh,
        soc_min_kwh,
        soc_max_kwh,
        headroom_t,
        t_hi,
        terminal_value,
        penalty_factor,
        reserve_kwh,
    )
    if milp_result is None:
        return _fallback_result(
            inputs, "HiGHS failed to solve the schedule; using maxuse fallback."
        )

    schedule = _build_schedule_from_milp(
        slots,
        milp_result,
        inputs.capacity_kwh,
        inputs.min_soc_pct,
        inputs.max_soc_pct,
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

    solar_reason = (
        f" while reserving headroom for forecast solar" if has_headroom else ""
    )

    return BatteryOptimizationResult(
        schedule=schedule,
        current_mode=current_mode,
        current_target_soc_pct=current_target,
        reason=(
            f"Optimized {horizon_hours:g}h price schedule with HiGHS "
            f"from {current_soc_pct:.1f}% SoC{solar_reason}"
            f"{clamp_reason}{load_reason}."
        ),
        optimized=True,
        solver="HIGHS",
        objective_value=milp_result.objective_value,
    )


@dataclass(slots=True)
class BatterySolverDebugResult:
    """Per-slot solver internals exposed only for test/debug validation.

    This structure is intentionally outside the public HA state attribute
    contract (see docs/battery_charge_mode_optimiser.md section 6.3) and
    exists to let deterministic tests assert on the underlying MILP flow
    variables (e.g. g_imp_t, g_exp_t, b_dis_load_t, b_dis_export_t, soc_t).
    """

    modes: list[str]
    soc_values: list[float]
    grid_import_kwh: list[float]
    grid_export_kwh: list[float]
    charge_grid_kwh: list[float]
    charge_pv_kwh: list[float]
    discharge_load_kwh: list[float]
    discharge_export_kwh: list[float]
    load_t: list[float]
    pv_t: list[float]
    t_hi: int
    reserve_kwh: float


def debug_solve_battery_schedule(
    inputs: BatteryOptimizationInputs,
) -> BatterySolverDebugResult | None:
    """Solve the MILP and return per-slot flow internals for test validation."""
    if (
        inputs.capacity_kwh is None
        or inputs.max_charge_power_w is None
        or inputs.max_discharge_power_w is None
        or inputs.current_soc_pct is None
        or inputs.min_soc_pct >= inputs.max_soc_pct
        or inputs.charge_efficiency <= 0
        or inputs.discharge_efficiency <= 0
    ):
        return None

    inputs = replace(inputs, horizon_hours=min(max(inputs.horizon_hours, 1.0), 48.0))
    slots = _normalize_slots(inputs)
    if not slots:
        return None

    solar_slots = _normalize_solar_slots(inputs)
    load_slots = _normalize_load_slots(inputs)
    pv_t = _energy_by_price_slot_from_power(slots, solar_slots)
    load_t = _energy_by_price_slot_from_energy(slots, load_slots)

    soc_min_kwh = inputs.capacity_kwh * (inputs.min_soc_pct / 100.0)
    soc_max_kwh = inputs.capacity_kwh * (inputs.max_soc_pct / 100.0)
    initial_soc_kwh = inputs.capacity_kwh * (inputs.current_soc_pct / 100.0)
    initial_soc_kwh = min(max(initial_soc_kwh, soc_min_kwh), soc_max_kwh)

    max_charge_kwh = [
        inputs.max_charge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]
    max_discharge_kwh = [
        inputs.max_discharge_power_w * slot.duration_hours / 1000.0 for slot in slots
    ]

    headroom_t, t_hi = _headroom_profile(pv_t, load_t, soc_min_kwh, soc_max_kwh)
    last_price = slots[-1].cost
    last_credit = slots[-1].credit
    terminal_value = _terminal_value_per_kwh(
        last_price, last_credit, inputs.discharge_efficiency
    )
    penalty_factor = _headroom_penalty_factor(last_price, last_credit)
    reserve_kwh = _terminal_reserve_kwh(
        inputs.capacity_kwh,
        soc_min_kwh,
        soc_max_kwh,
        initial_soc_kwh,
        inputs.charge_efficiency,
        max_charge_kwh,
    )

    milp_result = _solve_milp(
        inputs,
        slots,
        load_t,
        pv_t,
        max_charge_kwh,
        max_discharge_kwh,
        initial_soc_kwh,
        soc_min_kwh,
        soc_max_kwh,
        headroom_t,
        t_hi,
        terminal_value,
        penalty_factor,
        reserve_kwh,
    )
    if milp_result is None:
        return None

    return BatterySolverDebugResult(
        modes=[_MODE_ORDER[index] for index in milp_result.mode_indexes],
        soc_values=milp_result.soc_values,
        grid_import_kwh=milp_result.grid_import_values,
        grid_export_kwh=milp_result.grid_export_values,
        charge_grid_kwh=milp_result.charge_grid_values,
        charge_pv_kwh=milp_result.charge_pv_values,
        discharge_load_kwh=milp_result.discharge_load_values,
        discharge_export_kwh=milp_result.discharge_export_values,
        load_t=list(load_t),
        pv_t=list(pv_t),
        t_hi=t_hi,
        reserve_kwh=reserve_kwh,
    )
