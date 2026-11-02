"""Compact battery-mode diagnostic sensor for clocks and dashboards."""

from __future__ import annotations

import asyncio

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..util import format_compact_window
from .batterychargemodesensor import BatteryChargeModeSensor


class CompactBatteryModesSensor(SensorEntity):
    """Follow published plans, coalescing bursts and suppressing identical writes."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        source_sensor: BatteryChargeModeSensor,
    ) -> None:
        self._source_sensor = source_sensor
        self.entity_description = SensorEntityDescription(
            key="compactbatterymodes", translation_key="compactbatterymodes"
        )
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[self.entity_description.key]
        self._attr_suggested_object_id = self.entity_description.key
        self._attr_unique_id = build_sensor_unique_id(
            entry, self.entity_description.key
        )
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )
        self._pending_refresh: asyncio.Handle | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._compact: dict[str, str] = {}
        self._attr_native_value: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._source_sensor.async_add_update_listener(self._handle_source_update)
        )
        self.async_on_remove(self._cancel_updates)
        self._refresh()

    @callback
    def _handle_source_update(self) -> None:
        if self._pending_refresh is None:
            self._pending_refresh = self.hass.loop.call_soon(self._refresh)

    @callback
    def _refresh(self) -> None:
        if self._pending_refresh is not None:
            self._pending_refresh.cancel()
        self._pending_refresh = None
        if self._timer is not None:
            self._timer.cancel()
        now = dt_util.now().astimezone(
            dt_util.get_time_zone(self.hass.config.time_zone)
        )
        length, modes = self._source_sensor.build_modes_payload(now)
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        minutes, compact, delay = format_compact_window(modes, length, seconds)
        if compact != self._compact:
            self._attr_native_value = minutes
            self._compact = compact
            self.async_write_ha_state()
        self._timer = self.hass.loop.call_later(delay, self._refresh)

    @callback
    def _cancel_updates(self) -> None:
        if self._pending_refresh is not None:
            self._pending_refresh.cancel()
            self._pending_refresh = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return self._compact
