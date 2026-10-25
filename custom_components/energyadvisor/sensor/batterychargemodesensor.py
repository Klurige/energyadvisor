"""Battery Charge Mode sensor.

The schedule mirrors the price sensor's slots (same start and end times) and
copies each slot's cost and credit. The mode and target SoC of each slot come
from the battery planner (``battery_planner``), an exact dynamic programme
over the battery's state of charge that minimises the grid cost.

Planning runs in an executor job so that it never blocks the event loop. A
single worker task serves every trigger (new prices, a new household load
forecast, a clearly changed battery SoC and every quarter hour). Triggers that
arrive while a plan is being calculated are coalesced into one more run.

Solar and the water heater are not modelled. The household load forecast
already leaves out the water heater, and it is always on in the cheapest hours
of the day, so planning for it would not change the savings.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from ..battery_planner import (
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_STANDBY,
    BatteryParams,
    PlanSlot,
    plan_battery,
)
from ..const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_MAX_SOC_PCT,
    CONF_BATTERY_MIN_SOC_PCT,
    CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
    DOMAIN,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..coordinators.household_forecast_coordinator import (
    SLOT_MINUTES as FORECAST_SLOT_MINUTES,
)

if TYPE_CHECKING:
    from ..coordinators.household_forecast_coordinator import (
        HouseholdForecastCoordinator,
    )
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET_SOC_PCT = 50
DEFAULT_LOAD_KW = 1.5
DEFAULT_MIN_SOC_PCT = 5.0
DEFAULT_MAX_SOC_PCT = 100.0
DEFAULT_HORIZON_HOURS = 48.0
# The SoC entity only triggers a new plan when it moved this far since the
# last plan; smaller changes are picked up at the next quarter hour.
SOC_REPLAN_DELTA_PCT = 2.0
SOC_REPLAN_EPSILON = 1e-9
MIN_CURRENT_SLOT_HOURS = 1 / 60
SLOT_TIME_FORMAT = "%Y-%m-%dT%H:%M"
QUARTER_HOUR_MINUTES = (0, 15, 30, 45)
MODE_ICONS = {
    MODE_STANDBY: "mdi:battery-off",
    MODE_CHARGE: "mdi:battery-arrow-up",
    MODE_DISCHARGE: "mdi:battery-arrow-down",
}


@dataclass(frozen=True)
class ScheduleSlot:
    """A copy of one price slot plus the household load expected in it.

    ``cost`` is NaN when the price slot has no usable cost; the planner then
    leaves the battery alone in that slot.
    """

    start: datetime
    end: datetime
    cost: float
    credit: float
    load_kw: float


@dataclass(frozen=True)
class ScheduleInputs:
    """Everything ``build_schedule`` needs; safe to hand to an executor."""

    slots: tuple[ScheduleSlot, ...]
    params: BatteryParams | None
    start_soc_pct: float
    now: datetime
    horizon_hours: float


@dataclass(frozen=True)
class Schedule:
    """The planned ``modes``, the mode of the current slot and the SoC trace.

    ``soc_trace`` holds ``(slot end timestamp, planned end SoC %)`` per slot. It
    is empty when there is no battery to plan.
    """

    modes: list[dict[str, str | float]]
    current_mode: str
    soc_trace: tuple[tuple[float, float], ...]


def build_schedule(inputs: ScheduleInputs) -> Schedule:
    """Return one mode entry per price slot, planned with ``inputs.now``.

    Inputs:
        - Price slots in chronological order, each with its household load.
        - The battery parameters (``None`` when no battery is configured), the
          SoC at ``now``, ``now`` itself and the planning horizon in hours.
    Outputs:
        - A ``Schedule``. Slots that already ended are ``standby`` with the
          start SoC as target. The slot that contains ``now`` is planned for
          its remaining time (at least one minute). Slots starting after the
          horizon are ``standby`` and hold the last planned SoC. Without a
          battery every slot is ``standby`` with a fixed target of 50 %.
    Times are compared as timestamps, so the repeated hour when the clocks go
    back is handled correctly.
    """
    slots = inputs.slots
    modes = [MODE_STANDBY] * len(slots)
    params = inputs.params
    current_mode = MODE_STANDBY
    soc_trace: tuple[tuple[float, float], ...] = ()

    if params is None:
        targets: list[float] = [DEFAULT_TARGET_SOC_PCT] * len(slots)
    else:
        start_soc = min(
            max(inputs.start_soc_pct, params.min_soc_pct), params.max_soc_pct
        )
        now_ts = inputs.now.timestamp()
        horizon_ts = now_ts + inputs.horizon_hours * 3600
        bounds = [(slot.start.timestamp(), slot.end.timestamp()) for slot in slots]
        planned: list[int] = []
        plan_slots: list[PlanSlot] = []
        for index, (start_ts, end_ts) in enumerate(bounds):
            if end_ts <= now_ts or start_ts >= horizon_ts:
                continue
            hours = max(end_ts - max(start_ts, now_ts), 0.0) / 3600
            if start_ts <= now_ts:
                hours = max(hours, MIN_CURRENT_SLOT_HOURS)
            planned.append(index)
            plan_slots.append(
                PlanSlot(hours, slots[index].cost, slots[index].load_kw * hours)
            )

        result = plan_battery(plan_slots, params, start_soc)
        plan_position = {index: pos for pos, index in enumerate(planned)}
        targets = []
        soc = start_soc
        for index, (start_ts, end_ts) in enumerate(bounds):
            position = plan_position.get(index)
            if position is not None:
                modes[index] = result.modes[position]
                soc = result.end_soc_pct[position]
            targets.append(soc)
            if start_ts <= now_ts < end_ts:
                current_mode = modes[index]
        soc_trace = tuple(
            (end_ts, target) for (_, end_ts), target in zip(bounds, targets)
        )

    entries: list[dict[str, str | float]] = [
        {
            "from": slot.start.strftime(SLOT_TIME_FORMAT),
            "to": slot.end.strftime(SLOT_TIME_FORMAT),
            "mode": mode,
            "target_soc": target if params is None else round(target, 1),
            "cost": round(slot.cost, 2) if math.isfinite(slot.cost) else 0.0,
            "credit": round(slot.credit, 2),
        }
        for slot, mode, target in zip(slots, modes, targets)
    ]
    return Schedule(entries, current_mode, soc_trace)


class BatteryChargeModeSensor(SensorEntity):
    """Battery charge mode sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"modes"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        price_sensor: PriceSensor,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._price_sensor = price_sensor
        self._modes: list[dict[str, str | float]] = []
        self._current_mode = MODE_STANDBY
        self._soc_trace: tuple[tuple[float, float], ...] = ()
        self._current_soc_pct: float | None = None
        self._last_planned_soc_pct: float | None = None
        # One worker serves all triggers; they only set the flag.
        self._plan_task: asyncio.Task[None] | None = None
        self._replan_requested = False
        self._removed = False

        description = SensorEntityDescription(
            key="batterychargemode",
            translation_key="batterychargemode",
        )
        self.entity_description = description
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[description.key]
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = build_sensor_unique_id(entry, description.key)
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_worker)
        self._modes = self._default_modes()
        self._current_soc_pct = self._read_soc_pct()

        self.async_on_remove(
            self._price_sensor.async_add_update_listener(self._handle_price_update)
        )

        household = self._household_coordinator()
        if household is not None:
            household.register_update_callback(self._handle_household_update)
            self.async_on_remove(
                lambda: household.unregister_update_callback(
                    self._handle_household_update
                )
            )

        soc_entity_id = self._entry.options.get(CONF_BATTERY_SOC_ENTITY)
        if soc_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [soc_entity_id], self._handle_soc_change
                )
            )

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._handle_time_change,
                minute=QUARTER_HOUR_MINUTES,
                second=0,
            )
        )
        self._request_plan()

    @callback
    def _stop_worker(self) -> None:
        """Stop serving triggers; a plan being calculated is discarded."""
        self._removed = True
        self._replan_requested = False
        task, self._plan_task = self._plan_task, None
        if task is not None and not task.done():
            task.cancel()

    @callback
    def _handle_price_update(self) -> None:
        """Plan again whenever the price slots change."""
        self._request_plan()

    @callback
    def _handle_household_update(self) -> None:
        """Plan again whenever the household load forecast changes."""
        self._request_plan()

    @callback
    def _handle_time_change(self, _now: datetime) -> None:
        """Roll the plan forward at every quarter hour."""
        self._request_plan()

    @callback
    def _handle_soc_change(self, event: Event) -> None:
        """Store the new SoC and plan again once it has moved far enough."""
        new_state = event.data.get("new_state")
        soc = _parse_soc(new_state.state) if new_state is not None else None
        self._current_soc_pct = soc
        if soc is None:
            return
        last = self._last_planned_soc_pct
        if last is None or abs(soc - last) >= SOC_REPLAN_DELTA_PCT - SOC_REPLAN_EPSILON:
            self._request_plan()

    @callback
    def _request_plan(self) -> None:
        """Ask the worker for a new plan, starting it if it is idle."""
        if self._removed:
            return
        self._replan_requested = True
        if self._plan_task is not None and not self._plan_task.done():
            return
        self._plan_task = self.hass.async_create_background_task(
            self._async_plan_worker(), f"{DOMAIN} battery charge mode planner"
        )

    async def _async_plan_worker(self) -> None:
        """Plan until no request is pending.

        A plan is only published when no request arrived while it was being
        calculated; the next run then replaces it with fresher inputs.
        """
        while self._replan_requested and not self._removed:
            self._replan_requested = False
            try:
                inputs = self._build_inputs(self._now())
                schedule = await self.hass.async_add_executor_job(
                    build_schedule, inputs
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep serving later requests
                _LOGGER.exception("Battery charge mode planning failed")
                continue
            if self._replan_requested or self._removed:
                continue
            self._apply_schedule(schedule)

    @callback
    def _apply_schedule(self, schedule: Schedule) -> None:
        """Publish a new plan."""
        self._modes = schedule.modes
        self._current_mode = schedule.current_mode
        self._soc_trace = schedule.soc_trace
        self.async_write_ha_state()

    def _now(self) -> datetime:
        return dt_util.now()

    def _default_modes(self) -> list[dict[str, str | float]]:
        """Return the fixed schedule: every slot ``standby`` with target 50 %."""
        slots = self._price_slots({})
        return build_schedule(
            ScheduleInputs(tuple(slots), None, 0.0, self._now(), DEFAULT_HORIZON_HOURS)
        ).modes

    def _build_inputs(self, now: datetime) -> ScheduleInputs:
        """Copy the current prices, load forecast, SoC and options for planning."""
        params = self._battery_params()
        slots = self._price_slots(_load_by_start(self._household_forecast_slots()))
        soc = self._read_soc_pct()
        self._current_soc_pct = soc
        self._last_planned_soc_pct = soc
        if soc is None:
            soc = self._dead_reckoned_soc_pct(now)
        if soc is None:
            soc = params.min_soc_pct if params is not None else 0.0

        horizon = self._option_float(CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS)
        if horizon is None or horizon <= 0:
            horizon = DEFAULT_HORIZON_HOURS
        return ScheduleInputs(tuple(slots), params, soc, now, horizon)

    def _price_slots(self, loads: dict[str, list[float]]) -> list[ScheduleSlot]:
        """Copy the price sensor's slots that have both a start and an end."""
        slots = []
        for rate in getattr(self._price_sensor, "_rates", None) or []:
            start = rate.get("start")
            end = rate.get("end")
            if start is None or end is None:
                continue
            cost = _as_float(rate.get("cost", 0))
            credit = _as_float(rate.get("credit", 0))
            slots.append(
                ScheduleSlot(
                    start=start,
                    end=end,
                    cost=math.nan if cost is None else cost,
                    credit=0.0 if credit is None else credit,
                    load_kw=_slot_load_kw(start, end, loads),
                )
            )
        return slots

    def _household_coordinator(self) -> HouseholdForecastCoordinator | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        return getattr(runtime_data, "household_coordinator", None)

    def _household_forecast_slots(self) -> list[dict]:
        coordinator = self._household_coordinator()
        if coordinator is None:
            return []
        return list(getattr(coordinator, "forecast_slots", None) or [])

    def _option_float(self, key: str) -> float | None:
        """Return a numeric option, or ``None`` when unset or invalid."""
        return _as_float(self._entry.options.get(key))

    def _battery_params(self) -> BatteryParams | None:
        """Return the battery parameters, or ``None`` when not fully configured.

        Capacity and max charge power are required, the min SoC must be below
        the max SoC. The degradation cost is the wear cost per kWh delivered by
        the battery (0 when unset or invalid).
        """
        capacity = self._option_float(CONF_BATTERY_CAPACITY_KWH)
        charge_w = self._option_float(CONF_BATTERY_MAX_CHARGE_POWER_W)
        if capacity is None or capacity <= 0 or charge_w is None or charge_w <= 0:
            return None
        discharge_w = self._option_float(CONF_BATTERY_MAX_DISCHARGE_POWER_W)
        if discharge_w is None or discharge_w <= 0:
            discharge_w = charge_w
        min_soc = self._option_float(CONF_BATTERY_MIN_SOC_PCT)
        max_soc = self._option_float(CONF_BATTERY_MAX_SOC_PCT)
        min_soc = max(DEFAULT_MIN_SOC_PCT if min_soc is None else min_soc, 0.0)
        max_soc = min(DEFAULT_MAX_SOC_PCT if max_soc is None else max_soc, 100.0)
        if min_soc >= max_soc:
            return None
        wear = self._option_float(CONF_BATTERY_DEGRADATION_COST)
        return BatteryParams(
            capacity_kwh=capacity,
            min_soc_pct=min_soc,
            max_soc_pct=max_soc,
            max_charge_power_kw=charge_w / 1000,
            max_discharge_power_kw=discharge_w / 1000,
            wear_cost_per_kwh=max(wear or 0.0, 0.0),
        )

    def _read_soc_pct(self) -> float | None:
        """Return the SoC entity's state in percent, or ``None`` when unusable."""
        entity_id = self._entry.options.get(CONF_BATTERY_SOC_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        return _parse_soc(state.state) if state is not None else None

    def _dead_reckoned_soc_pct(self, now: datetime) -> float | None:
        """Return the previous plan's SoC at the end of the slot before ``now``."""
        now_ts = now.timestamp()
        for end_ts, soc in reversed(self._soc_trace):
            if end_ts <= now_ts:
                return soc
        return None

    @property
    def icon(self) -> str:
        return MODE_ICONS.get(self._current_mode, MODE_ICONS[MODE_STANDBY])

    @property
    def state(self) -> str:
        return self._current_mode

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "modes": self._modes,
            "current_soc_pct": (
                None
                if self._current_soc_pct is None
                else round(self._current_soc_pct, 1)
            ),
        }


def _as_float(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` (bools are not numbers)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_soc(value: object) -> float | None:
    """Parse a SoC state in percent; unknown, unavailable and junk give ``None``."""
    if value in (None, STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return _as_float(str(value).replace("%", "").replace(",", ".").strip())


def _load_by_start(forecast_slots: list[dict]) -> dict[str, list[float]]:
    """Group the forecast's average loads (kW) by local slot start.

    A start can occur twice on the day the clocks go back.
    """
    loads: dict[str, list[float]] = {}
    for slot in forecast_slots:
        if not isinstance(slot, Mapping):
            continue
        start = slot.get("from")
        load_kw = _as_float(slot.get("load"))
        if isinstance(start, str) and load_kw is not None:
            loads.setdefault(start, []).append(max(load_kw, 0.0))
    return loads


def _slot_load_kw(
    start: datetime, end: datetime, loads: dict[str, list[float]]
) -> float:
    """Return the average load of a price slot from the forecast slots it covers.

    Forecast slots are matched by local start time. A price slot longer than
    one forecast slot averages the ones it finds; with none the default load is
    used.
    """
    minutes = (end.timestamp() - start.timestamp()) / 60
    count = max(1, round(minutes / FORECAST_SLOT_MINUTES))
    first = start.replace(tzinfo=None)
    values: list[float] = []
    for i in range(count):
        key = (first + timedelta(minutes=FORECAST_SLOT_MINUTES * i)).strftime(
            SLOT_TIME_FORMAT
        )
        values.extend(loads.get(key, ()))
    return sum(values) / len(values) if values else DEFAULT_LOAD_KW
