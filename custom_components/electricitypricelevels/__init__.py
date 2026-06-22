"""The ElectricityPriceLevel integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, LOGGER, PREFERRED_SENSOR_ENTITY_IDS, build_sensor_unique_id
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


def _registry_entry_sensor_key(entity_entry: er.RegistryEntry) -> str | None:
    """Resolve the known sensor key for an entity-registry entry."""
    if entity_entry.translation_key in PREFERRED_SENSOR_ENTITY_IDS:
        return entity_entry.translation_key

    _, _, suffix = entity_entry.unique_id.rpartition("_")
    if suffix in PREFERRED_SENSOR_ENTITY_IDS:
        return suffix

    return None


async def _async_migrate_entity_registry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Align entity IDs with the preferred names and stabilize unique IDs."""
    registry = er.async_get(hass)

    @callback
    def _update_entry(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        sensor_key = _registry_entry_sensor_key(entity_entry)
        if sensor_key is None:
            return None

        updates: dict[str, str] = {}

        new_unique_id = build_sensor_unique_id(entry, sensor_key)
        if entity_entry.unique_id != new_unique_id:
            updates["new_unique_id"] = new_unique_id

        preferred_entity_id = PREFERRED_SENSOR_ENTITY_IDS[sensor_key]
        entity_id_owner = registry.async_get(preferred_entity_id)
        if entity_entry.entity_id != preferred_entity_id:
            if entity_id_owner is None or entity_id_owner.id == entity_entry.id:
                updates["new_entity_id"] = preferred_entity_id
            else:
                LOGGER.warning(
                    "Keeping %s because preferred entity ID %s is already used by %s.",
                    entity_entry.entity_id,
                    preferred_entity_id,
                    entity_id_owner.entity_id,
                )

        return updates or None

    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        updates = _update_entry(entity_entry)
        if not updates:
            continue

        new_entity_id = updates.get("new_entity_id")
        if new_entity_id and registry.async_get(new_entity_id) is None:
            if hass.states.get(new_entity_id) is not None:
                # Clear stale restored states so the registry rename can claim the
                # preferred entity ID during staged naming migrations.
                hass.states.async_remove(new_entity_id)

        registry.async_update_entity(entity_entry.entity_id, **updates)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ElectricityPriceLevel from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)
    await _async_migrate_entity_registry(hass, entry)

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
