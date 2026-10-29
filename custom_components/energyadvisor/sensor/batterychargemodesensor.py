"""Battery Charge Mode sensor.

The schedule mirrors the price sensor's slots (same start and end times) and
copies each slot's cost and credit. The mode and target SoC of each slot come
from the battery planner (``battery_planner``), an exact dynamic programme
over the battery's state of charge that minimises the grid cost.

Every price slot that has not ended is planned, however many the price sensor
holds (about a day, or two once tomorrow's prices are known). The plan is rolled
forward and extended whenever new prices arrive. The slot that contains "now" is
planned as a whole slot, so its mode stays stable until the slot ends.

Slots that already ended keep the mode and target SoC the sensor published
while they were running. Those records are stored with the entity's restore
data, so they survive a restart.

The sensor's ``target_soc`` attribute is the target SoC of the slot that
contains "now", so it is always available next to the ``modes``. Every
published ``target_soc`` is an integer percent, always rounded up so that the
battery keeps the higher level.

The planner knows ``standby``, ``charge``, ``pvcharge``, ``discharge`` and
``sell``.
``discharge`` covers the household load and never exports; ``sell`` also
exports to the grid when the slot's credit makes that profitable. Selling is
always part of the plan when a battery is configured. A planned ``discharge`` in
daylight is published as ``maxuse``: the battery then covers the household load,
which leaves any solar surplus for charging the battery. A ``sell`` is never
turned into ``maxuse``, which would stop the export. Solar production is not
modelled beyond the solar forecast's load offset and surplus.

Costs are the price sensor's ``cost`` and credits its ``credit`` (fees, taxes
and VAT included), not the raw spot price, and the battery's wear cost is in the
same unit. The credit is the revenue per exported kWh; a slot whose credit is
unknown, zero or negative never sells.

The known prices end at some point (today, or tomorrow once its prices are
published), and the plan has to value the energy still in the battery then. It
is worth what it saves later, the median cost of the planned slots at the
battery's efficiency, less the wear. Without that value the battery would be
sold off at the end of the known prices for any positive credit, before
tomorrow's prices are known.

Planning runs in an executor job so that it never blocks the event loop. Only
an immutable snapshot of the inputs (``ScheduleInputs``) is handed to the
thread. A single worker task serves every trigger (new prices, a new household
load forecast, a clearly changed battery SoC and every quarter hour). Triggers
that arrive while a plan is being calculated are coalesced into one more run.

Solar and the water heater are not modelled. The household load forecast
already leaves out the water heater, and it is always on in the cheapest hours
of the day, so planning for it would not change the savings.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from ..battery_planner import (
    MODE_CHARGE,
    MODE_DISCHARGE,
    MODE_PVCHARGE,
    MODE_SELL,
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
    from ..coordinators.solar_forecast_coordinator import SolarForecastCoordinator
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET_SOC_PCT = 50
DEFAULT_LOAD_KW = 1.0
DEFAULT_MIN_SOC_PCT = 5.0
DEFAULT_MAX_SOC_PCT = 100.0
# The SoC entity only triggers a new plan when it moved this far since the
# last plan; smaller changes are picked up at the next quarter hour.
SOC_REPLAN_DELTA_PCT = 2.0
SOC_REPLAN_EPSILON = 1e-9
SLOT_TIME_FORMAT = "%Y-%m-%dT%H:%M"
QUARTER_HOUR_MINUTES = (0, 15, 30, 45)
# Daytime discharge: the battery covers the load and leaves solar to the grid
# or the battery's own charging; the planner models it as plain discharge.
MODE_MAXUSE = "maxuse"
MODES = (
    MODE_STANDBY,
    MODE_CHARGE,
    MODE_PVCHARGE,
    MODE_DISCHARGE,
    MODE_SELL,
    MODE_MAXUSE,
)
MODE_ICONS = {
    MODE_STANDBY: "mdi:battery-off",
    MODE_CHARGE: "mdi:battery-arrow-up",
    MODE_PVCHARGE: "mdi:solar-power",
    MODE_DISCHARGE: "mdi:battery-arrow-down",
    MODE_SELL: "mdi:transmission-tower-export",
    MODE_MAXUSE: "mdi:battery-sync",
}
# How long the modes of ended slots are remembered; the price sensor never
# holds older slots.
HISTORY_KEEP = timedelta(days=2)
HISTORY_RESTORE_KEY = "history"


@dataclass(frozen=True)
class ScheduleSlot:
    """A copy of one price slot plus the household load expected in it.

    ``cost`` is NaN when the price slot has no usable cost; the planner then
    leaves the battery alone in that slot. ``credit`` is the revenue per
    exported kWh; a credit that is unknown, zero or negative never sells.
    """

    start: datetime
    end: datetime
    cost: float
    credit: float
    load_kw: float
    solar_surplus_kwh: float = 0.0
    solar_load_offset_kwh: float = 0.0


@dataclass(frozen=True)
class SlotRecord:
    """The mode and target SoC published for one slot, keyed by its start."""

    start: float
    mode: str
    target_soc: float


@dataclass(frozen=True)
class ScheduleInputs:
    """Everything ``build_schedule`` needs; safe to hand to an executor.

    ``history`` holds the records of slots that have run; slots that ended
    without a record are ``standby``. ``daylight`` holds ``(sunrise, sunset)``
    timestamp windows; a planned discharge in the middle of a window becomes
    ``maxuse``, a planned sell stays a sell. ``params.terminal_value_per_kwh`` is
    what the energy left in the battery after the last slot is worth.
    """

    slots: tuple[ScheduleSlot, ...]
    params: BatteryParams | None
    start_soc_pct: float
    now: datetime
    history: tuple[SlotRecord, ...] = ()
    daylight: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Schedule:
    """The planned ``modes``, the mode of the current slot and the SoC trace.

    ``soc_trace`` holds ``(slot end timestamp, planned end SoC %)`` per slot. It
    is empty when there is no battery to plan. ``current`` is the record of the
    slot that contains ``now`` (``None`` without a battery or such a slot).
    ``current_target_soc`` is that slot's published ``target_soc`` (the fixed
    50 % without a battery); it is ``None`` only when no slot contains ``now``.
    The published ``target_soc`` values are whole percent, rounded up; the
    ``soc_trace`` and ``current`` keep the exact planned SoC.
    """

    modes: list[dict[str, str | float]]
    current_mode: str
    soc_trace: tuple[tuple[float, float], ...]
    current: SlotRecord | None = None
    current_target_soc: float | None = None


@dataclass(frozen=True)
class _StoredHistory(ExtraStoredData):
    """The slot records saved with the entity's restore state."""

    records: tuple[SlotRecord, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            HISTORY_RESTORE_KEY: [
                [record.start, record.mode, record.target_soc]
                for record in self.records
            ]
        }


def build_schedule(inputs: ScheduleInputs) -> Schedule:
    """Return one mode entry per price slot, planned with ``inputs.now``.

    Inputs:
        - Price slots in chronological order, each with its household load.
        - The battery parameters (``None`` when no battery is configured), the
          SoC at ``now`` and ``now`` itself.
    Outputs:
        - A ``Schedule``. Slots that already ended take the mode and target of
          their ``history`` record, or are ``standby`` (carrying the SoC on)
          when there is none. Every other slot is planned, so the plan spans
          all the slots given. The slot that contains ``now`` is planned as a
          whole slot starting from the SoC at ``now``; the mode then does not
          change as the slot runs out, and its target SoC is scaled to the time
          that is left. A planned discharge in daylight is ``maxuse``; a planned
          sell is never changed. Without
          a battery every slot is ``standby`` with a fixed target of 50 %.
          ``current_target_soc`` is the target of the slot that contains
          ``now``.
    Times are compared as timestamps, so the repeated hour when the clocks go
    back is handled correctly. It touches nothing but ``inputs`` and is safe to
    run in a worker thread.
    """
    slots = inputs.slots
    modes = [MODE_STANDBY] * len(slots)
    params = inputs.params
    current_mode = MODE_STANDBY
    current: SlotRecord | None = None
    soc_trace: tuple[tuple[float, float], ...] = ()
    now_ts = inputs.now.timestamp()
    bounds = [(slot.start.timestamp(), slot.end.timestamp()) for slot in slots]
    current_index = next(
        (
            index
            for index, (start_ts, end_ts) in enumerate(bounds)
            if start_ts <= now_ts < end_ts
        ),
        None,
    )

    if params is None:
        targets: list[float] = [DEFAULT_TARGET_SOC_PCT] * len(slots)
    else:

        def clamp(soc: float) -> float:
            return min(max(soc, params.min_soc_pct), params.max_soc_pct)

        start_soc = clamp(inputs.start_soc_pct)
        history = {record.start: record for record in inputs.history}
        planned: list[int] = []
        plan_slots: list[PlanSlot] = []
        for index, (start_ts, end_ts) in enumerate(bounds):
            if end_ts <= now_ts:
                continue
            hours = max(end_ts - start_ts, 0.0) / 3600
            planned.append(index)
            plan_slots.append(
                PlanSlot(
                    hours,
                    slots[index].cost,
                    max(
                        slots[index].load_kw * hours
                        - slots[index].solar_load_offset_kwh,
                        0.0,
                    ),
                    slots[index].credit,
                    slots[index].solar_surplus_kwh,
                )
            )

        result = plan_battery(plan_slots, params, start_soc)
        plan_position = {index: pos for pos, index in enumerate(planned)}
        end_socs = list(result.end_soc_pct)

        # The current slot was planned as a whole slot from the SoC at now, so
        # its real end SoC is only the share of the change that is still left,
        # and every later target moves by the part that has already elapsed.
        shift = 0.0
        if current_index is not None:
            start_ts, end_ts = bounds[current_index]
            position = plan_position[current_index]
            elapsed = (now_ts - start_ts) / (end_ts - start_ts)
            change = end_socs[position] - start_soc
            end_socs[position] = start_soc + change * (1 - elapsed)
            shift = -change * elapsed

        targets = []
        soc = start_soc
        for index, (start_ts, end_ts) in enumerate(bounds):
            position = plan_position.get(index)
            if position is not None:
                mode = result.modes[position]
                # Only a discharge becomes maxuse: maxuse never exports.
                if mode == MODE_DISCHARGE and _in_daylight(
                    start_ts, end_ts, inputs.daylight
                ):
                    mode = MODE_MAXUSE
                modes[index] = mode
                moved = 0.0 if index == current_index else shift
                soc = clamp(end_socs[position] + moved)
            elif (record := history.get(start_ts)) is not None:
                modes[index] = record.mode
                soc = clamp(record.target_soc)
            targets.append(soc)
            if index == current_index:
                current_mode = modes[index]
                current = SlotRecord(start_ts, current_mode, soc)
        soc_trace = tuple(
            (end_ts, target) for (_, end_ts), target in zip(bounds, targets)
        )

    entries: list[dict[str, str | float]] = [
        {
            "from": slot.start.strftime(SLOT_TIME_FORMAT),
            "to": slot.end.strftime(SLOT_TIME_FORMAT),
            "mode": mode,
            "target_soc": _publish_soc(target),
            "cost": round(slot.cost, 2) if math.isfinite(slot.cost) else 0.0,
            "credit": round(slot.credit, 2),
        }
        for slot, mode, target in zip(slots, modes, targets)
    ]
    current_target = (
        None if current_index is None else entries[current_index]["target_soc"]
    )
    return Schedule(entries, current_mode, soc_trace, current, current_target)


class BatteryChargeModeSensor(RestoreEntity, SensorEntity):
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
        self._history: dict[float, SlotRecord] = {}
        self._current_target_soc: float | None = None
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
        self._history = _parse_history(await self.async_get_last_extra_data())
        default = self._default_schedule()
        self._modes = default.modes
        self._current_target_soc = default.current_target_soc

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

        solar = self._solar_coordinator()
        if solar is not None:
            solar.register_update_callback(self._handle_solar_update)
            self.async_on_remove(
                lambda: solar.unregister_update_callback(self._handle_solar_update)
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
    def _handle_solar_update(self) -> None:
        """Plan again whenever the solar forecast changes."""
        self._request_plan()

    @callback
    def _handle_time_change(self, _now: datetime) -> None:
        """Roll the plan forward at every quarter hour."""
        self._request_plan()

    @callback
    def _handle_soc_change(self, event: Event) -> None:
        """Plan again once the SoC has moved far enough from the last plan's."""
        new_state = event.data.get("new_state")
        soc = _parse_soc(new_state.state) if new_state is not None else None
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
        """Publish a new plan and remember the current slot's mode."""
        self._modes = schedule.modes
        self._current_mode = schedule.current_mode
        self._current_target_soc = schedule.current_target_soc
        self._soc_trace = schedule.soc_trace
        current = schedule.current
        if current is not None:
            self._history[current.start] = current
            oldest = current.start - HISTORY_KEEP.total_seconds()
            self._history = {
                start: record
                for start, record in self._history.items()
                if start >= oldest
            }
        self.async_write_ha_state()

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Save the modes of the slots that have run, for the next start."""
        return _StoredHistory(
            tuple(sorted(self._history.values(), key=lambda record: record.start))
        )

    def _now(self) -> datetime:
        return dt_util.now()

    def _default_schedule(self) -> Schedule:
        """Return the fixed schedule: every slot ``standby`` with target 50 %."""
        slots = self._price_slots({})
        return build_schedule(ScheduleInputs(tuple(slots), None, 0.0, self._now()))

    def _build_inputs(self, now: datetime) -> ScheduleInputs:
        """Copy the current prices, load forecast, SoC and options for planning."""
        params = self._battery_params()
        slots = self._price_slots(
            _load_by_start(self._household_forecast_slots()),
            self._solar_forecast_intervals(),
        )
        if params is not None:
            params = replace(
                params,
                terminal_value_per_kwh=_terminal_value_per_kwh(params, slots, now),
            )
        soc = self._read_soc_pct()
        self._last_planned_soc_pct = soc
        if soc is None:
            soc = self._dead_reckoned_soc_pct(now)
        if soc is None:
            soc = params.min_soc_pct if params is not None else 0.0
        return ScheduleInputs(
            tuple(slots),
            params,
            soc,
            now,
            history=tuple(self._history.values()),
            daylight=self._daylight_windows(slots),
        )

    def _daylight_windows(
        self, slots: list[ScheduleSlot]
    ) -> tuple[tuple[float, float], ...]:
        """Return the sunrise-to-sunset window of every day the slots start on.

        A day without a sunrise or sunset (polar day or night) has no window,
        so none of its slots counts as daytime.
        """
        windows = []
        for day in sorted({slot.start.date() for slot in slots}):
            sunrise = get_astral_event_date(self.hass, "sunrise", day)
            sunset = get_astral_event_date(self.hass, "sunset", day)
            if sunrise is not None and sunset is not None and sunrise < sunset:
                windows.append((sunrise.timestamp(), sunset.timestamp()))
        return tuple(windows)

    def _price_slots(
        self,
        loads: dict[str, list[float]],
        solar_intervals: tuple[tuple[float, float, float], ...] = (),
    ) -> list[ScheduleSlot]:
        """Copy the price sensor's slots that have both a start and an end."""
        slots = []
        for rate in getattr(self._price_sensor, "_rates", None) or []:
            start = rate.get("start")
            end = rate.get("end")
            if start is None or end is None:
                continue
            cost = _as_float(rate.get("cost", 0))
            credit = _as_float(rate.get("credit", 0))
            load_kw = _slot_load_kw(start, end, loads)
            solar_surplus_kwh, solar_load_offset_kwh = _solar_energy_for_slot(
                start, end, load_kw, solar_intervals
            )
            slots.append(
                ScheduleSlot(
                    start=start,
                    end=end,
                    cost=math.nan if cost is None else cost,
                    credit=0.0 if credit is None else credit,
                    load_kw=load_kw,
                    solar_surplus_kwh=solar_surplus_kwh,
                    solar_load_offset_kwh=solar_load_offset_kwh,
                )
            )
        return slots

    def _solar_coordinator(self) -> SolarForecastCoordinator | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        return getattr(runtime_data, "solar_coordinator", None)

    def _solar_forecast_intervals(self) -> tuple[tuple[float, float, float], ...]:
        """Snapshot solar forecasts as UTC (start, end, power-kW) intervals."""
        coordinator = self._solar_coordinator()
        if coordinator is None:
            return ()
        try:
            local_tz = ZoneInfo(self.hass.config.time_zone)
        except (AttributeError, TypeError, ValueError, ZoneInfoNotFoundError):
            return ()

        intervals = []
        for item in getattr(coordinator, "forecast", None) or ():
            if not isinstance(item, Mapping):
                continue
            end_value = item.get("end")
            power_kw = _as_float(item.get("pow"))
            if not isinstance(end_value, str) or power_kw is None:
                continue
            try:
                period_end = datetime.fromisoformat(end_value)
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=local_tz)
                end_ts = period_end.astimezone(timezone.utc).timestamp()
            except (OverflowError, OSError, ValueError):
                continue
            intervals.append(
                (
                    end_ts - FORECAST_SLOT_MINUTES * 60,
                    end_ts,
                    max(power_kw, 0.0),
                )
            )
        return tuple(intervals)

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
        return {"modes": self._modes, "target_soc": self._current_target_soc}


def _terminal_value_per_kwh(
    params: BatteryParams, slots: list[ScheduleSlot], now: datetime
) -> float:
    """Return what a kWh stored in the battery is worth after the last slot.

    It is the median cost of the slots that are planned (those that have not
    ended and have a usable cost) at the battery's discharge efficiency, less
    the wear cost, and never below zero. It is zero when no slot has a cost.
    """
    now_ts = now.timestamp()
    costs = [
        slot.cost
        for slot in slots
        if slot.end.timestamp() > now_ts and math.isfinite(slot.cost)
    ]
    if not costs:
        return 0.0
    eta = math.sqrt(params.round_trip_efficiency)
    return max(eta * statistics.median(costs) - params.wear_cost_per_kwh, 0.0)


def _publish_soc(soc: float) -> int:
    """Return the SoC in percent as an integer, always rounded up.

    Rounding up keeps a higher level in the battery. The rounding to 6
    decimals first keeps float noise (30.000000000000004) from adding a point.
    """
    return math.ceil(round(soc, 6))


def _in_daylight(
    start_ts: float, end_ts: float, windows: tuple[tuple[float, float], ...]
) -> bool:
    """Return whether the middle of the slot lies between a sunrise and sunset."""
    middle = (start_ts + end_ts) / 2
    return any(sunrise <= middle < sunset for sunrise, sunset in windows)


def _parse_history(data: ExtraStoredData | None) -> dict[float, SlotRecord]:
    """Return the slot records saved in ``data``, skipping malformed ones."""
    stored = data.as_dict() if data is not None else None
    raw = stored.get(HISTORY_RESTORE_KEY) if isinstance(stored, Mapping) else None
    history: dict[float, SlotRecord] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        start = _as_float(item[0])
        mode = item[1]
        target = _as_float(item[2])
        if start is None or target is None or mode not in MODES:
            continue
        history[start] = SlotRecord(start, mode, target)
    return history


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


def _solar_energy_for_slot(
    start: datetime,
    end: datetime,
    load_kw: float,
    intervals: tuple[tuple[float, float, float], ...],
) -> tuple[float, float]:
    """Return forecast solar surplus and household load offset in kWh."""
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    surplus_kwh = 0.0
    load_offset_kwh = 0.0
    for solar_start, solar_end, power_kw in intervals:
        overlap_h = max(0.0, min(end_ts, solar_end) - max(start_ts, solar_start)) / 3600
        if overlap_h == 0:
            continue
        load_offset_kwh += min(power_kw, load_kw) * overlap_h
        surplus_kwh += max(power_kw - load_kw, 0.0) * overlap_h
    return surplus_kwh, load_offset_kwh
