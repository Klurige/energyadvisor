"""Battery Charge Mode sensor and planner.

The planner consumes the Energy Advisor price schedule and solar forecast,
and produces a battery charge mode schedule with the same granularity
as the price sensor (typically 15 minutes).
The modes are 
  * maxuse - Charge the battery if solar surplus, use battery if not.
  * charge - Charge the battery, but only consume solar and grid.
  * discharge - Consume battery. Any solar surplus could be exported.
  * sell - Discharge the battery at full power. Export the surplus.
  * standby - Do not use the battery. Consume solar and grid.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_BATTERY_DEGRADATION_COST,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

from .chargemodehelpers import find_current_mode, default_modes, find_peaks_in_modes

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

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        price_sensor: PriceSensor,
    ) -> None:
        self._entry = entry
        self._price_sensor = price_sensor
        self._modes = default_modes()
        self._current_mode = find_current_mode(self._modes).get("mode", "unknown")
        self._remove_source_listener = None

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
        _LOGGER.debug(f"Battery charge mode sensor registering listener with price sensor")
        self._remove_source_listener = self._price_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)

    def _handle_source_update(self) -> None:
        _LOGGER.debug("Prices coming in - updating battery charge mode")
        self.calculate_battery_mode()
        _LOGGER.debug(f"Sending out modes: {self._current_mode}")
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        return MODE_ICONS.get(self._current_mode, "mdi:battery-unknown")

    @property
    def state(self) -> str | None:
        return self._current_mode

    @property
    def extra_state_attributes(self) -> dict:
       return {"modes": self._modes}

    def calculate_battery_mode(self):
        # Calculate the battery mode.
        # If modes are missing for any price period, calculate the base mode. Then update the current mode.
        if not self._modes:
            self._modes = default_modes()

        now = dt_util.now()
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        modes_kv = {mode.get("from"): mode for mode in self._modes}

        _LOGGER.debug(f"Calculating battery mode at {now}, today_midnight: {today_midnight}")
        if self._price_sensor is not None:
            _LOGGER.debug(f"Got price sensor: {self._price_sensor.entity_id}, state: {self._price_sensor.state}")
            rates = self._price_sensor._rates
            if rates is not None:
                _LOGGER.debug(f"Got rates from price sensor.")
                # Build the modes based on the price data.
                for rate in rates:
                    slot_start = rate.get("start")
                    if not isinstance(slot_start, datetime):
                        _LOGGER.warning(
                            "Skipping rate without a valid start timestamp: %s", rate
                        )
                        continue
                    if slot_start.tzinfo is None:
                        slot_start = slot_start.replace(tzinfo=now.tzinfo)
                    else:
                        slot_start = dt_util.as_local(slot_start)
                    slot_end = rate.get("end")
                    if not isinstance(slot_end, datetime):
                        _LOGGER.warning(
                            "Skipping rate without a valid end timestamp: %s", rate
                        )
                        continue
                    if slot_end.tzinfo is None:
                        slot_end = slot_end.replace(tzinfo=now.tzinfo)
                    else:
                        slot_end = dt_util.as_local(slot_end)
                    #if slot_end < now:
                    #    continue

                    slot_from = slot_start.strftime("%Y-%m-%dT%H:%M")
                    slot_cost = rate.get("cost")
                    slot_credit = rate.get("credit")
                    modes_kv[slot_from] = {"from": slot_from, "mode": "unknown", "cost": slot_cost, "credit": slot_credit}

        modes = [modes_kv[key] for key in sorted(modes_kv)]
        margin = self._attr_exclude_from_recording = self._entry.options.get(CONF_BATTERY_DEGRADATION_COST, True)
        peaks = find_peaks_in_modes(modes, margin)
        _LOGGER.debug(f"Found peaks in battery modes: {peaks}")
        for peak in peaks:
            peak_from = peak.get("from")
            if peak_from in modes_kv:
                modes_kv[peak_from]["mode"] = "sell"
        self._modes = [modes_kv[key] for key in sorted(modes_kv)]
        self._current_mode = find_current_mode(self._modes).get("mode", "unknown")

