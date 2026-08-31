"""Battery Charge Mode sensor and planner."""

from __future__ import annotations

import logging
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
    optimize_battery_schedule,
)
from ..const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_MAX_SOC_PCT,
    CONF_BATTERY_MIN_SOC_PCT,
    CONF_BATTERY_OPTIMIZATION_ENABLED,
    CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from .chargemodehelpers import default_modes, find_current_mode

if TYPE_CHECKING:
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

MODE_ICONS = {
    "standby": "mdi:battery-off",
    "charge": "mdi:battery-charging",
    "maxuse": "mdi:battery-check",
    "discharge": "mdi:battery-minus",
    "sell": "mdi:battery-arrow-up",
    "unknown": "mdi:battery-unknown",
}


class BatteryChargeModeSensor(SensorEntity):
    """Battery charge mode sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"charge_entries", "modes"})

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
        self._optimization_enabled = bool(
            entry.options.get(CONF_BATTERY_OPTIMIZATION_ENABLED, False)
        )
        optimization_horizon = entry.options.get(CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS)
        self._optimization_horizon_hours = (
            float(optimization_horizon) if optimization_horizon is not None else 48.0
        )
        self._battery_capacity_kwh = entry.options.get(CONF_BATTERY_CAPACITY_KWH)
        self._battery_max_charge_power_w = entry.options.get(
            CONF_BATTERY_MAX_CHARGE_POWER_W
        )
        self._battery_max_discharge_power_w = entry.options.get(
            CONF_BATTERY_MAX_DISCHARGE_POWER_W
        ) or self._battery_max_charge_power_w
        battery_min_soc_pct = entry.options.get(CONF_BATTERY_MIN_SOC_PCT)
        battery_max_soc_pct = entry.options.get(CONF_BATTERY_MAX_SOC_PCT)
        self._battery_min_soc_pct = (
            float(battery_min_soc_pct) if battery_min_soc_pct is not None else 5.0
        )
        self._battery_max_soc_pct = (
            float(battery_max_soc_pct) if battery_max_soc_pct is not None else 95.0
        )
        self._current_soc_pct: float | None = None
        self._current_target_soc_pct: float | None = None
        self._reason = "Waiting for electricity price data."
        self._solver: str | None = None
        self._remove_source_listener = None
        self._remove_soc_listener = None
        self._modes = default_modes()
        self._current_mode = find_current_mode(self._modes).get("mode", "unknown")

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

        if self._optimization_enabled and self._battery_soc_entity_id:
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

        self.calculate_battery_mode()
        self.async_write_ha_state()

    @callback
    def _handle_soc_update(self, _event) -> None:
        """Recompute the schedule when the battery SoC changes."""
        self.calculate_battery_mode()
        self.async_write_ha_state()

    def _handle_source_update(self) -> None:
        """Recompute the schedule when the price sensor changes."""
        _LOGGER.debug("Prices coming in - updating battery charge mode")
        self.calculate_battery_mode()
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
            "charge_entries": self._modes,
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

    def _build_optimizer_inputs(self) -> BatteryOptimizationInputs:
        """Build the request object used by the optimizer."""
        price_rates = list(getattr(self._price_sensor, "_rates", []) or [])
        current_soc_pct = (
            self._read_current_soc_pct() if self._optimization_enabled else None
        )
        self._current_soc_pct = current_soc_pct
        return BatteryOptimizationInputs(
            rates=price_rates,
            reference_time=dt_util.now(),
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
        )

    def calculate_battery_mode(self) -> None:
        """Calculate the battery schedule and current mode."""
        if not self._modes:
            self._modes = default_modes()

        optimizer_inputs = self._build_optimizer_inputs()
        result = optimize_battery_schedule(optimizer_inputs)

        self._modes = result.schedule or default_modes()
        self._current_mode = result.current_mode
        self._current_target_soc_pct = result.current_target_soc_pct
        self._reason = result.reason
        self._solver = result.solver

        if not self._current_mode:
            self._current_mode = find_current_mode(self._modes).get("mode", "unknown")
