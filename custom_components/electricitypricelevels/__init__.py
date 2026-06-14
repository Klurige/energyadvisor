"""The ElectricityPriceLevel integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .models import ElectricityPriceLevelsRuntimeData
from .services import async_setup_services

PLATFORMS = [Platform.SENSOR]

_OLD_CONF_NORDPOOL_AREA_ID = "nordpool_area_id"
_NEW_CONF_NORDPOOL_PRICES_SENSOR = "nordpool_prices_sensor"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry from older versions."""
    if entry.version == 1:
        LOGGER.info("Migrating config entry from version 1 to 2")
        new_options = dict(entry.options)
        new_data = dict(entry.data)

        # Rename nordpool_area_id → nordpool_prices_sensor
        # Old format stored area code ("se3"), new format stores full entity_id
        if _OLD_CONF_NORDPOOL_AREA_ID in new_options:
            area_id = new_options.pop(_OLD_CONF_NORDPOOL_AREA_ID)
            if _NEW_CONF_NORDPOOL_PRICES_SENSOR not in new_options:
                new_options[_NEW_CONF_NORDPOOL_PRICES_SENSOR] = (
                    f"sensor.nord_pool_{area_id.lower()}_current_price"
                )
        if _OLD_CONF_NORDPOOL_AREA_ID in new_data:
            area_id = new_data.pop(_OLD_CONF_NORDPOOL_AREA_ID)
            if _NEW_CONF_NORDPOOL_PRICES_SENSOR not in new_data:
                new_data[_NEW_CONF_NORDPOOL_PRICES_SENSOR] = (
                    f"sensor.nord_pool_{area_id.lower()}_current_price"
                )

        # Reset price_divisor to 1 — the value 100 was stored but never used
        # in v1, so applying it now would break prices by 100×
        new_options["price_divisor"] = 1

        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=2
        )
        LOGGER.info("Migration to version 2 complete")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ElectricityPriceLevel from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, ElectricityPriceLevelsRuntimeData):
        hass.data[DOMAIN][entry.entry_id] = runtime_data

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove services when last entry is unloaded
    if result:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, "get_levels")

    return result


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
