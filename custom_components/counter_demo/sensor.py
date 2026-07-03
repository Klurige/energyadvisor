"""Cycle counter sensor for the Counter Demo integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, PREFERRED_SENSOR_ENTITY_IDS, build_sensor_unique_id


class CounterDemoSensor(SensorEntity):
    """Count from 0 to 100 and back to 0 every second.

    Inputs:
        - Config entry metadata used for the entity identity.
        - A one-second timer from Home Assistant.
    Outputs:
        - State: the current counter value as an integer between 0 and 100.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self.entity_description = SensorEntityDescription(
            key="cycle_counter",
            translation_key="cycle_counter",
        )
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[self.entity_description.key]
        self._attr_suggested_object_id = self.entity_description.key
        self._attr_unique_id = build_sensor_unique_id(
            entry, self.entity_description.key
        )
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = True
        self._value = 0
        self._counting_up = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(seconds=1)
            )
        )

    @callback
    def _handle_tick(self, _now) -> None:
        if self._counting_up:
            if self._value >= 100:
                self._counting_up = False
                self._value -= 1
            else:
                self._value += 1
        else:
            if self._value <= 0:
                self._counting_up = True
                self._value += 1
            else:
                self._value -= 1
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Counter Demo sensor platform."""
    device_info = DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Counter Demo",
        manufacturer="Counter Demo",
        model="Counter Demo",
    )
    async_add_entities([CounterDemoSensor(hass, entry, device_info)], True)
