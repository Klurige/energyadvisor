"""Household base-load forecast sensor."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..coordinators.household_forecast_coordinator import HouseholdForecastCoordinator


class HouseholdForecastSensor(SensorEntity):
    """Expose the learned household base-load forecast."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        coordinator: HouseholdForecastCoordinator,
    ) -> None:
        self._entry = entry
        self._coordinator = coordinator
        description = SensorEntityDescription(
            key="base_load",
            translation_key="base_load",
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
        self._coordinator.register_update_callback(self._handle_coordinator_update)
        self._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_update_callback(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the learned base load in kW."""
        base_load_kw = self._coordinator.base_load_kw
        if base_load_kw is None:
            return None
        return round(base_load_kw, 3)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the learned sample summary for dashboards and automations."""
        household_base_load_w = self._coordinator.household_base_load_w
        last_sample_kw = self._coordinator.last_sample_kw
        return {
            "household_base_load_w": (
                round(household_base_load_w, 1)
                if household_base_load_w is not None
                else None
            ),
            "learning_nights": self._coordinator.learning_nights,
            "data_since": self._coordinator.data_since,
            "last_sample_date": self._coordinator.last_sample_date,
            "last_sample_kw": round(last_sample_kw, 3) if last_sample_kw is not None else None,
            "reason": self._coordinator.reason,
        }


BaseLoadSensor = HouseholdForecastSensor
