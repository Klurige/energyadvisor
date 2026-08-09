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
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

if TYPE_CHECKING:
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)


class BatteryChargeModeSensor(SensorEntity):
    """Battery charge mode sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        source_sensor: PriceSensor,
    ) -> None:
        self._entry = entry
        self._source_sensor = source_sensor
        self._attr_native_value = None
        self._attr_icon = "mdi:battery"
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
        self._remove_source_listener = self._source_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)

    def _handle_source_update(self) -> None:
        _LOGGER.debug("Prices coming in - updating battery charge mode")
        self._attr_native_value = "standby"
        mode_icons = {
            "standby": "mdi:battery-inactive",
            "charge": "mdi:battery-charging",
            "maxuse": "mdi:battery-check",
            "discharge": "mdi:battery-minus",
            "sell": "mdi:battery-arrow-up",
        }
        self._attr_icon = mode_icons.get(self._attr_native_value, "mdi:battery-unknown")
        _LOGGER.debug(f"Sending out modes: {self._attr_native_value}")
        self.async_write_ha_state()

    @property
    def state(self) -> str | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict:
       """Return modes schedule matching price sensor rates."""
       modes = []
       for rate in self._source_sensor._rates:
           start = rate.get("start")
           if start is None:
               continue
           modes.append(
               {
                   "from": start.strftime("%Y-%m-%dT%H:%M"),
                   "mode": self._attr_native_value or "standby",
                   "cost": rate.get("cost"),
               }
           )
       return {"modes": modes}
