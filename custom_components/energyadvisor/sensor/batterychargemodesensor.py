"""Battery Charge Mode sensor.

Placeholder planner: the schedule mirrors the price sensor's slots (same start
and end times), copies each slot's cost and credit, and assigns the ``standby``
mode and a fixed target SoC to every slot.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

if TYPE_CHECKING:
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

MODE_STANDBY = "standby"
DEFAULT_TARGET_SOC_PCT = 50
SLOT_TIME_FORMAT = "%Y-%m-%dT%H:%M"


class BatteryChargeModeSensor(SensorEntity):
    """Battery charge mode sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:battery-off"
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
        self._modes = self._build_modes()
        self.async_on_remove(
            self._price_sensor.async_add_update_listener(self._handle_price_update)
        )

    @callback
    def _handle_price_update(self) -> None:
        """Rebuild the schedule whenever the price slots change."""
        self._modes = self._build_modes()
        self.async_write_ha_state()

    def _build_modes(self) -> list[dict[str, str | float]]:
        """Return one ``standby`` entry per price slot.

        Times, cost and credit are taken from the price slot; cost and credit
        are rounded to 2 decimals.
        """
        modes: list[dict[str, str | float]] = []
        for rate in getattr(self._price_sensor, "_rates", None) or []:
            start = rate.get("start")
            end = rate.get("end")
            if start is None or end is None:
                continue
            modes.append(
                {
                    "from": start.strftime(SLOT_TIME_FORMAT),
                    "to": end.strftime(SLOT_TIME_FORMAT),
                    "mode": MODE_STANDBY,
                    "target_soc": DEFAULT_TARGET_SOC_PCT,
                    "cost": round(rate.get("cost", 0), 2),
                    "credit": round(rate.get("credit", 0), 2),
                }
            )
        return modes

    @property
    def state(self) -> str:
        return MODE_STANDBY

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"modes": self._modes}
