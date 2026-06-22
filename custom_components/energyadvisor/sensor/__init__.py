"""Support for the ElectricityPriceLevel sensor service."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration

from ..const import (
    CONF_FORECAST_ENTITY,
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_POWER_ENTITY,
    DOMAIN,
)
from ..models import ElectricityPriceLevelsRuntimeData
from .batterychargemodesensor import BatteryChargeModeSensor
from ..solar_forecast_coordinator import SolarForecastCoordinator
from .compactlevels import CompactLevelsSensor
from .electricitypricelevels import ElectricityPriceLevelsSensor
from .nordpool_coordinator import NordpoolDataCoordinator
from .solarforecastsensor import SolarForecastSensor

_LOGGER = logging.getLogger(__name__)


def _resolve_nordpool_config_entry_id(
    hass: HomeAssistant, prices_sensor_entity_id: str
) -> str | None:
    """Resolve the Nord Pool config entry used by the selected prices sensor."""
    entity_registry = er.async_get(hass)
    entity_entry = entity_registry.async_get(prices_sensor_entity_id)
    if entity_entry and entity_entry.config_entry_id:
        return entity_entry.config_entry_id

    nordpool_entries = hass.config_entries.async_entries("nordpool")
    if len(nordpool_entries) == 1:
        fallback_entry = nordpool_entries[0]
        _LOGGER.warning(
            "Could not resolve config entry for %s from the entity registry; "
            "falling back to the only loaded Nord Pool entry %s.",
            prices_sensor_entity_id,
            fallback_entry.entry_id,
        )
        return fallback_entry.entry_id

    _LOGGER.error(
        "Could not resolve which Nord Pool config entry owns %s.",
        prices_sensor_entity_id,
    )
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    user_language = hass.config.language
    translations = await async_get_translations(
        hass, user_language, "device_info", [DOMAIN]
    )
    integration = await async_get_integration(hass, DOMAIN)
    device_name = translations.get(
        f"component.{DOMAIN}.device_info.device_name", "Untranslated device name"
    )
    manufacturer = translations.get(
        f"component.{DOMAIN}.device_info.manufacturer", "Untranslated manufacturer"
    )
    model = translations.get(
        f"component.{DOMAIN}.device_info.model", "Untranslated model"
    )

    device_info = DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, entry.entry_id)},
        name=device_name,
        manufacturer=manufacturer,
        model=model,
        sw_version=str(integration.version) if integration.version else None,
        configuration_url=None,
    )

    levels_sensor = ElectricityPriceLevelsSensor(hass, entry, device_info)
    compact_levels_sensor = CompactLevelsSensor(hass, entry, device_info, levels_sensor)
    battery_sensor = BatteryChargeModeSensor(hass, entry, device_info, levels_sensor)
    entities = [levels_sensor, compact_levels_sensor, battery_sensor]

    solar_coordinator: SolarForecastCoordinator | None = None
    solar_sensor: SolarForecastSensor | None = None
    forecast_entity = entry.options.get(CONF_FORECAST_ENTITY)
    power_entity = entry.options.get(CONF_POWER_ENTITY)
    if forecast_entity and power_entity:
        solar_coordinator = SolarForecastCoordinator(hass, entry, levels_sensor)
        await solar_coordinator.async_setup()
        solar_sensor = SolarForecastSensor(hass, entry, device_info, solar_coordinator)
        entities.append(solar_sensor)

    prices_sensor_entity_id = entry.options.get(CONF_NORDPOOL_PRICES_SENSOR, "")
    nordpool_config_entry_id_to_use = _resolve_nordpool_config_entry_id(
        hass, prices_sensor_entity_id
    )
    if nordpool_config_entry_id_to_use is None:
        if solar_coordinator is not None:
            await solar_coordinator.async_shutdown()
        return

    currency_from_config = entry.options.get("currency", "") or None
    coordinator = NordpoolDataCoordinator(
        hass,
        nordpool_config_entry_id_to_use,
        levels_sensor.async_update_data,
        currency_from_config,
    )

    runtime_data = ElectricityPriceLevelsRuntimeData(
        levels_sensor=levels_sensor,
        compact_sensor=compact_levels_sensor,
        coordinator=coordinator,
        solar_sensor=solar_sensor,
        solar_coordinator=solar_coordinator,
    )
    entry.runtime_data = runtime_data

    async_add_entities(entities, True)
    coordinator.start()

    @callback
    def _async_cleanup_nordpool_task(_event=None) -> None:
        _LOGGER.debug("Cleaning up Nordpool coordinator on unload.")
        coordinator.stop()
        if solar_coordinator is not None:
            hass.async_create_task(solar_coordinator.async_shutdown())

    entry.async_on_unload(_async_cleanup_nordpool_task)
