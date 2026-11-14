"""Binary sensor platform for Energy Advisor."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .models import EnergyAdvisorRuntimeData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the water-heater schedule binary sensor."""
    runtime_data = getattr(entry, "runtime_data", None)
    if not isinstance(runtime_data, EnergyAdvisorRuntimeData):
        _LOGGER.error(
            "Water-heater schedule setup skipped because sensor runtime data "
            "is unavailable."
        )
        return
    if runtime_data.water_heater_sensor is None:
        raise RuntimeError("Water-heater schedule sensor was not initialized")

    async_add_entities([runtime_data.water_heater_sensor], True)
