"""Battery Charge Mode sensor and planner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..battery_optimizer import (
    BatteryOptimizationInputs,
    BatteryOptimizationResult,
    OptimizationSnapshot,
    household_forecast_slots_to_load_forecasts,
    optimize_battery_schedule_if_changed,
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
from .chargemodehelpers import default_modes, find_current_mode

if TYPE_CHECKING:
    from .price import PriceSensor
    from ..coordinators.household_forecast_coordinator import (
        HouseholdForecastCoordinator,
    )
    from ..coordinators.solar_forecast_coordinator import SolarForecastCoordinator

_LOGGER = logging.getLogger(__name__)

MODE_ICONS = {
    "standby": "mdi:battery-off",
    "charge": "mdi:battery-arrow-up",
    "maxuse": "mdi:battery-sync",
    "discharge": "mdi:battery-arrow-down",
    "sell": "mdi:battery-minus",
    "unknown": "mdi:battery-unknown",
}


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
        self._battery_soc_entity_id = entry.options.get(CONF_BATTERY_SOC_ENTITY) or None
        optimization_horizon = entry.options.get(
            CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS
        )
        self._optimization_horizon_hours = (
            float(optimization_horizon) if optimization_horizon is not None else 48.0
        )
        self._battery_capacity_kwh = entry.options.get(CONF_BATTERY_CAPACITY_KWH)
        self._battery_max_charge_power_w = entry.options.get(
            CONF_BATTERY_MAX_CHARGE_POWER_W
        )
        self._battery_max_discharge_power_w = (
            entry.options.get(CONF_BATTERY_MAX_DISCHARGE_POWER_W)
            or self._battery_max_charge_power_w
        )
        battery_min_soc_pct = entry.options.get(CONF_BATTERY_MIN_SOC_PCT)
        battery_max_soc_pct = entry.options.get(CONF_BATTERY_MAX_SOC_PCT)
        self._battery_min_soc_pct = (
            float(battery_min_soc_pct) if battery_min_soc_pct is not None else 5.0
        )
        self._battery_max_soc_pct = (
            float(battery_max_soc_pct) if battery_max_soc_pct is not None else 95.0
        )
        battery_degradation_cost = entry.options.get(CONF_BATTERY_DEGRADATION_COST)
        self._battery_degradation_cost = (
            float(battery_degradation_cost)
            if battery_degradation_cost is not None
            else 0.0
        )
        self._current_soc_pct: float | None = None
        self._current_target_soc_pct: float | None = None
        self._optimization_enabled = True
        self._reason = "Waiting for electricity price data."
        self._solver: str | None = None
        self._remove_source_listener = None
        self._remove_soc_listener = None
        self._solar_coordinator: SolarForecastCoordinator | None = None
        self._household_coordinator: HouseholdForecastCoordinator | None = None
        self._modes = default_modes()
        self._current_mode = find_current_mode(self._modes).get("mode", "unknown")
        # Solving runs off the event loop in a single worker task. Triggers only
        # set the flag; the worker loops until no request arrived during a solve.
        self._solve_task: asyncio.Task[None] | None = None
        self._recalc_requested = False
        self._last_solved_snapshot: OptimizationSnapshot | None = None

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
        _LOGGER.debug(
            "Battery charge mode sensor registering listener with price sensor"
        )
        self._remove_source_listener = self._price_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)

        runtime_data = getattr(self._entry, "runtime_data", None)
        self._solar_coordinator = getattr(runtime_data, "solar_coordinator", None)
        if self._solar_coordinator is not None:
            solar_coordinator = self._solar_coordinator
            _LOGGER.debug(
                "Battery charge mode sensor registering listener for solar forecast"
            )
            solar_coordinator.register_update_callback(self._handle_source_update)

            def _remove_solar_listener() -> None:
                solar_coordinator.unregister_update_callback(self._handle_source_update)

            self.async_on_remove(_remove_solar_listener)

        self._household_coordinator = getattr(
            runtime_data, "household_coordinator", None
        )
        if self._household_coordinator is not None:
            household_coordinator = self._household_coordinator
            _LOGGER.debug(
                "Battery charge mode sensor registering listener for household forecast"
            )
            household_coordinator.register_update_callback(self._handle_source_update)

            def _remove_household_listener() -> None:
                household_coordinator.unregister_update_callback(
                    self._handle_source_update
                )

            self.async_on_remove(_remove_household_listener)

        if self._battery_soc_entity_id:
            _LOGGER.debug(
                "Battery charge mode sensor registering listener for SoC entity %s",
                self._battery_soc_entity_id,
            )
            self._remove_soc_listener = async_track_state_change_event(
                self.hass,
                [self._battery_soc_entity_id],
                self._handle_soc_update,
            )
            self.async_on_remove(self._remove_soc_listener)

        self._request_recalculation()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the solver worker; a solve already running is discarded."""
        self._recalc_requested = False
        task, self._solve_task = self._solve_task, None
        if task is not None and not task.done():
            task.cancel()

    @callback
    def _handle_soc_update(self, _event) -> None:
        """Show the new SoC at once and request a (gated) recalculation."""
        soc_pct = self._read_current_soc_pct()
        if soc_pct != self._current_soc_pct:
            self._current_soc_pct = soc_pct
            self.async_write_ha_state()
        self._request_recalculation()

    @callback
    def _handle_source_update(self) -> None:
        """Request a recalculation when prices or forecasts change."""
        _LOGGER.debug("Prices or forecasts changed - requesting battery mode update")
        self._request_recalculation()

    @callback
    def _request_recalculation(self) -> None:
        """Ask the worker to recalculate, starting it if it is idle."""
        self._recalc_requested = True
        if self._solve_task is not None and not self._solve_task.done():
            return
        self._solve_task = self.hass.async_create_background_task(
            self._async_recalculation_worker(),
            f"{DOMAIN} battery charge mode solver",
        )

    async def _async_recalculation_worker(self) -> None:
        """Serve recalculation requests one at a time until none are pending."""
        while self._recalc_requested:
            self._recalc_requested = False
            try:
                applied = await self.async_calculate_battery_mode()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep serving later requests
                _LOGGER.exception("Battery charge mode calculation failed")
                continue
            if applied:
                _LOGGER.debug("Sending out modes: %s", self._current_mode)
                self.async_write_ha_state()

    @property
    def icon(self) -> str:
        return MODE_ICONS.get(self._current_mode, "mdi:battery-unknown")

    @property
    def state(self) -> str | None:
        return self._current_mode

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "modes": self._modes,
            "current_soc_pct": (
                round(self._current_soc_pct, 1)
                if self._current_soc_pct is not None
                else None
            ),
            "current_target_soc": self._current_target_soc_pct,
            "optimization_enabled": self._optimization_enabled,
            "reason": self._reason,
            "solver": self._solver,
        }

    def _read_current_soc_pct(self) -> float | None:
        """Read the current SoC percentage from Home Assistant state."""
        if not self._battery_soc_entity_id or self.hass is None:
            return None

        state = self.hass.states.get(self._battery_soc_entity_id)
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

        try:
            return float(str(state.state).replace("%", "").replace(",", "."))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Skipping invalid battery SoC value from %s: %s",
                self._battery_soc_entity_id,
                state.state,
            )
            return None

    def _build_optimizer_inputs(
        self, reference_time: datetime | None = None
    ) -> BatteryOptimizationInputs:
        """Build the request object used by the optimizer."""
        reference_time = reference_time or dt_util.now()
        # The optimizer runs in an executor thread, so hand it copies.
        price_rates = [
            dict(rate) if isinstance(rate, Mapping) else rate
            for rate in getattr(self._price_sensor, "_rates", []) or []
        ]
        solar_forecasts = self._read_solar_forecasts()
        current_soc_pct = self._read_current_soc_pct()
        self._current_soc_pct = current_soc_pct
        return BatteryOptimizationInputs(
            rates=price_rates,
            reference_time=reference_time,
            current_soc_pct=current_soc_pct,
            capacity_kwh=(
                float(self._battery_capacity_kwh)
                if self._battery_capacity_kwh is not None
                else None
            ),
            max_charge_power_w=(
                float(self._battery_max_charge_power_w)
                if self._battery_max_charge_power_w is not None
                else None
            ),
            max_discharge_power_w=(
                float(self._battery_max_discharge_power_w)
                if self._battery_max_discharge_power_w is not None
                else None
            ),
            min_soc_pct=self._battery_min_soc_pct,
            max_soc_pct=self._battery_max_soc_pct,
            horizon_hours=self._optimization_horizon_hours,
            optimization_enabled=self._optimization_enabled,
            solar_forecasts=solar_forecasts,
            load_forecasts=self._read_load_forecasts(reference_time),
            degradation_cost=self._battery_degradation_cost,
        )

    def _read_solar_forecasts(self) -> list[dict[str, object]]:
        """Read solar forecast slots from the registered solar coordinator."""
        if self._solar_coordinator is None:
            return []
        forecasts = getattr(self._solar_coordinator, "forecast", []) or []
        return [dict(entry) for entry in forecasts if isinstance(entry, dict)]

    def _read_load_forecasts(
        self, reference_time: datetime | None = None
    ) -> list[dict[str, object]]:
        """Read household load forecast slots from the registered coordinator.

        Adapts ``HouseholdForecastCoordinator.forecast_slots`` (authoritative
        48-hour load forecast) into the optimizer's
        ``{"start", "end", "load_kwh"}`` shape.
        """
        if self._household_coordinator is None:
            return []
        forecast_slots = (
            getattr(self._household_coordinator, "forecast_slots", []) or []
        )
        return household_forecast_slots_to_load_forecasts(
            forecast_slots, reference_time or dt_util.now()
        )

    def _merge_schedule_with_elapsed_modes(
        self, schedule: list[dict[str, object]], reference_time: datetime
    ) -> list[dict[str, object]]:
        """Keep completed slots while replacing the current quarter-hour slot."""
        current_slot_start = reference_time.replace(
            minute=(reference_time.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        elapsed_modes: dict[datetime, dict[str, object]] = {}
        for mode in self._modes:
            slot_from = dt_util.parse_datetime(mode.get("from"))
            if slot_from is None:
                continue
            if slot_from.tzinfo is None:
                slot_from = slot_from.replace(tzinfo=reference_time.tzinfo)
            else:
                slot_from = dt_util.as_local(slot_from)
            slot_start = slot_from.replace(
                minute=(slot_from.minute // 15) * 15,
                second=0,
                microsecond=0,
            )
            if (
                slot_start.date() == reference_time.date()
                and slot_start < current_slot_start
                and slot_start not in elapsed_modes
            ):
                elapsed_mode = dict(mode)
                elapsed_mode["from"] = slot_start.strftime("%Y-%m-%dT%H:%M")
                elapsed_modes[slot_start] = elapsed_mode

        merged_modes = dict(elapsed_modes)
        for mode in schedule:
            slot_from = dt_util.parse_datetime(mode.get("from"))
            if slot_from is None:
                continue
            if slot_from.tzinfo is None:
                slot_from = slot_from.replace(tzinfo=reference_time.tzinfo)
            else:
                slot_from = dt_util.as_local(slot_from)
            slot_start = slot_from.replace(
                minute=(slot_from.minute // 15) * 15,
                second=0,
                microsecond=0,
            )
            if slot_start >= current_slot_start:
                current_mode = dict(mode)
                current_mode["from"] = slot_start.strftime("%Y-%m-%dT%H:%M")
                merged_modes[slot_start] = current_mode

        today_midnight = reference_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if not any(slot_from == today_midnight for slot_from in merged_modes):
            merged_modes[today_midnight] = default_modes(reference_time)[0]

        return [merged_modes[slot_from] for slot_from in sorted(merged_modes)]

    async def async_calculate_battery_mode(self) -> bool:
        """Recalculate the schedule off the event loop and apply the result.

        Returns ``True`` when a new schedule was applied. Returns ``False``
        when the inputs did not change enough to justify a solve, or when the
        15-minute slot rolled over mid-solve; the latter schedules a fresh
        solve rather than applying a schedule that starts in the past.
        """
        if not self._modes:
            self._modes = default_modes()

        reference_time = dt_util.now()
        optimizer_inputs = self._build_optimizer_inputs(reference_time)
        snapshot, result = await self.hass.async_add_executor_job(
            optimize_battery_schedule_if_changed,
            optimizer_inputs,
            self._last_solved_snapshot,
        )
        if result is None:
            return False
        if _slot_start(dt_util.now()) != _slot_start(reference_time):
            _LOGGER.debug("Discarding battery schedule solved for a past slot")
            self._recalc_requested = True
            return False

        self._apply_result(result, reference_time)
        self._last_solved_snapshot = snapshot
        return True

    def _apply_result(
        self, result: BatteryOptimizationResult, reference_time: datetime
    ) -> None:
        """Publish an optimizer result as the sensor's schedule and state."""
        self._modes = self._merge_schedule_with_elapsed_modes(
            result.schedule or default_modes(reference_time),
            reference_time,
        )
        self._current_mode = result.current_mode
        self._current_target_soc_pct = result.current_target_soc_pct
        self._reason = result.reason
        if self._household_coordinator is not None:
            quality = getattr(self._household_coordinator, "quality_status", None)
            if quality in ("degraded", "stale", "fallback"):
                self._reason += (
                    f" Household load forecast quality: {quality}. "
                    f"{self._household_coordinator.reason}"
                )
        self._solver = result.solver

        if not self._current_mode:
            self._current_mode = find_current_mode(self._modes).get("mode", "unknown")


def _slot_start(value: datetime) -> datetime:
    """Return the start of the 15-minute slot containing ``value``."""
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)
