from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from ..const import CONF_EXCLUDE_FROM_RECORDING, DOMAIN

if TYPE_CHECKING:
    from .electricitypricelevels import ElectricityPriceLevelsSensor

_LOGGER = logging.getLogger(__name__)

# Simulation settings for testing without real data
# Set simulationLevelIndex to 0 to activate simulation, -1 to use real data
simulationLevelIndex = -1
# Each level represents this many minutes
simulation_level_length_minutes = 60
# Update period in seconds. Every time these many seconds have passed, the level index
# is incremented, meaning the "current" level moves forward by
# simulation_level_length_minutes
simulation_update_seconds = 20

# Simulation levels. A static string. Edit it for you particular simulation pattern.
simulation_levels = ""
if simulationLevelIndex >= 0:
    simulation_levels = "LLLLLMMMHMMLLLLMMHHHMLLLLLLLLMMMHMMLLLLMMHHHMLLL"


def _resolve_levels_sensor(
    hass: HomeAssistant, entity_id: str | None = None
) -> ElectricityPriceLevelsSensor | None:
    """Resolve a levels sensor instance from runtime data."""
    runtime_entries = hass.data.get(DOMAIN, {})
    matches: list[ElectricityPriceLevelsSensor] = []

    for runtime_data in runtime_entries.values():
        levels_sensor = getattr(runtime_data, "levels_sensor", None)
        if levels_sensor is None:
            continue
        if entity_id is None or levels_sensor.entity_id == entity_id:
            matches.append(levels_sensor)

    if entity_id is not None:
        return matches[0] if matches else None
    if len(matches) == 1:
        return matches[0]
    return None


def calculate_levels(
    hass: HomeAssistant,
    requested_length: int = 0,
    fill_unknown: bool = False,
    entity_id: str | None = None,
) -> dict[str, int | float | str | None]:
    """Build compact levels from the main sensor instance."""
    levels_sensor = _resolve_levels_sensor(hass, entity_id)
    if levels_sensor is None:
        return {
            "level_length": 0,
            "levels": "",
            "low_threshold": None,
            "high_threshold": None,
        }
    return levels_sensor.build_levels_payload(
        requested_length=requested_length,
        fill_unknown=fill_unknown,
    )


class CompactLevelsSensor(SensorEntity):
    """Entity that exposes the latest electricity price levels as an attribute."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        source_sensor: ElectricityPriceLevelsSensor,
    ) -> None:
        self._entry = entry
        self._source_sensor = source_sensor
        description = SensorEntityDescription(
            key="compactlevels",
            translation_key="compactlevels",
        )
        self.entity_description = description
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )
        self._task: asyncio.Task | None = None
        self._remove_source_listener = None
        self._service_compact: dict[str, str] | None = None
        self._service_seconds: int | None = None
        self._waiting_for_first_value = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_source_listener = self._source_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)
        if self._source_sensor.has_rates:
            await self._start_levels_sensor()

    def _handle_source_update(self) -> None:
        if self.hass is None:
            return
        if self._waiting_for_first_value:
            self.hass.async_create_task(self._start_levels_sensor())
            return
        self.hass.async_create_task(self._refresh_from_source())

    async def _start_levels_sensor(self) -> None:
        if not self._waiting_for_first_value:
            return
        self._waiting_for_first_value = False
        await self._refresh_from_source()
        self._task = self.hass.async_create_background_task(
            self._periodic_update(), "compactlevels_periodic_update"
        )

    async def _refresh_from_source(self) -> int:
        self._service_seconds, self._service_compact, next_update = (
            self._fetch_compact_values()
        )
        self.async_write_ha_state()
        return next_update

    async def async_will_remove_from_hass(self) -> None:
        if self._task:
            self._task.cancel()
        await super().async_will_remove_from_hass()

    async def _periodic_update(self) -> None:
        while True:
            try:
                next_update = await self._refresh_from_source()
            except Exception:
                _LOGGER.exception("Error in compact levels periodic update")
                next_update = 60
            await asyncio.sleep(next_update)

    def _fetch_compact_values(self) -> tuple[int, dict[str, str], int]:
        global simulationLevelIndex, simulation_levels
        global simulation_level_length_minutes, simulation_update_seconds

        if simulationLevelIndex >= 0:
            result = {
                "level_length": simulation_level_length_minutes,
                "levels": simulation_levels,
            }
            seconds_since_midnight = (
                simulationLevelIndex * simulation_level_length_minutes * 60
            ) % 86400
            simulationLevelIndex = (simulationLevelIndex + 1) % len(simulation_levels)
        else:
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            now_local = datetime.now(local_tz)
            seconds_since_midnight = int(
                (
                    now_local
                    - now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                ).total_seconds()
            )
            result = self._source_sensor.build_levels_payload(reference_time=now_local)

        levels_str = str(result.get("levels", ""))
        level_length = int(result.get("level_length", 0) or 0)
        minutes_since_midnight = seconds_since_midnight / 60
        minutes_into_period = (
            minutes_since_midnight % level_length if level_length > 0 else 0
        )

        if simulationLevelIndex >= 0:
            next_update_seconds = simulation_update_seconds
        else:
            next_update_seconds = (
                int((level_length - minutes_into_period) * 60)
                if levels_str and level_length > 0
                else 5
            )

        passed_levels = ""
        future_levels = ""
        if levels_str and level_length > 0:
            current_level_index = int(minutes_since_midnight / level_length)
            levels_in_1_hour = int(60 / level_length) if level_length > 0 else 0
            levels_in_12_hours = int(12 * levels_in_1_hour)

            passed_end = current_level_index
            passed_start = max(0, passed_end - levels_in_1_hour)
            future_start = current_level_index
            future_end = future_start + levels_in_12_hours

            passed_levels = levels_str[passed_start:passed_end]
            future_levels = levels_str[future_start:future_end]

            if len(passed_levels) < levels_in_1_hour:
                passed_levels = (
                    "U" * (levels_in_1_hour - len(passed_levels)) + passed_levels
                )
            if len(future_levels) < levels_in_12_hours:
                future_levels += "U" * (levels_in_12_hours - len(future_levels))

        compact = f"{int(minutes_since_midnight)}:{level_length}:{passed_levels}:{future_levels}"
        value = {"compact": compact}

        _LOGGER.debug(
            "Minutes: %s Compact: %s, next update in seconds: %s",
            int(minutes_since_midnight),
            value,
            next_update_seconds,
        )
        return (
            int(minutes_since_midnight),
            value,
            max(1, int(next_update_seconds + 0.5)),
        )

    @property
    def state(self) -> int | str:
        if self._service_compact is not None:
            return (
                self._service_seconds
                if self._service_seconds is not None
                else "Unknown"
            )
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return self._service_compact or {}
