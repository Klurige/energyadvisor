"""The Energy Advisor integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, LOGGER, PREFERRED_SENSOR_ENTITY_IDS, build_sensor_unique_id
from .models import EnergyAdvisorRuntimeData
from .grid_resale_http import GridResaleExportView
from .grid_resale import CONF_RESALE_ENABLED, migrate_resale_options
from .services import (
    async_remove_debug_services,
    async_setup_debug_services,
    async_setup_services,
    async_setup_resale_services,
)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

_OLD_CONF_NORDPOOL_AREA_ID = "nordpool_area_id"
_NEW_CONF_NORDPOOL_PRICES_SENSOR = "nordpool_prices_sensor"
_LEGACY_SENSOR_KEYS: dict[str, str] = {
    "base_load": "load_forecast",
    "waterheaterschedule": "waterheater",
}


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
    translation_key = entity_entry.translation_key
    if translation_key in PREFERRED_SENSOR_ENTITY_IDS:
        return translation_key
    if translation_key in _LEGACY_SENSOR_KEYS:
        return _LEGACY_SENSOR_KEYS[translation_key]

    _, _, suffix = entity_entry.unique_id.rpartition("_")
    if suffix in PREFERRED_SENSOR_ENTITY_IDS:
        return suffix
    if suffix in _LEGACY_SENSOR_KEYS:
        return _LEGACY_SENSOR_KEYS[suffix]

    preferred_entity_id = PREFERRED_SENSOR_ENTITY_IDS.get("price")
    if preferred_entity_id and entity_entry.entity_id == preferred_entity_id:
        return "price"

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
        sensor_key = _registry_entry_sensor_key(entity_entry)
        if sensor_key == "waterheater" and entity_entry.domain == "sensor":
            registry.async_remove(entity_entry.entity_id)
            continue

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
    """Set up Energy Advisor from a config entry."""
    options, needs_review = migrate_resale_options(dict(entry.options))
    if options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=options)
    issue_id = f"grid_resale_shared_sources_{entry.entry_id}"
    if needs_review:
        LOGGER.warning(
            "Grid resale disabled for %s: previous resale sources or battery polarity "
            "conflict with the shared inputs. Verify the signed battery, grid and solar "
            "settings, then enable grid resale again. The ledger is retained.",
            entry.entry_id,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="grid_resale_shared_sources",
        )
    elif options.get(CONF_RESALE_ENABLED):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)
    async_setup_resale_services(hass)
    if (
        entry.options.get(CONF_RESALE_ENABLED)
        and "energyadvisor_grid_resale_http" not in hass.data
    ):
        hass.http.register_view(GridResaleExportView(hass))
        hass.data["energyadvisor_grid_resale_http"] = True
    await _async_migrate_entity_registry(hass, entry)

    # The binary schedule entity is created by the sensor platform and shared
    # with its diagnostics sensor, so set up the platforms in dependency order.
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.BINARY_SENSOR]
    )
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, EnergyAdvisorRuntimeData):
        hass.data[DOMAIN][entry.entry_id] = runtime_data
        if runtime_data.debug_coordinator is not None:
            async_setup_debug_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove services when last entry is unloaded
    if result:
        runtime_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        resale = getattr(runtime_data, "grid_resale_coordinator", None)
        if resale is not None:
            await resale.async_shutdown()
        remaining = hass.data.get(DOMAIN, {})
        if not remaining:
            hass.services.async_remove(DOMAIN, "get_levels")
            hass.services.async_remove(DOMAIN, "export_grid_resale")
            hass.services.async_remove(DOMAIN, "import_grid_resale_history")
        if getattr(runtime_data, "debug_coordinator", None) is not None and not any(
            getattr(data, "debug_coordinator", None) is not None
            for data in remaining.values()
        ):
            async_remove_debug_services(hass)

    return result


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
