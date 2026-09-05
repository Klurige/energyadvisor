"""Household Load forecast sensor."""

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
    ATTR_FORECASTS,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..coordinators.household_forecast_coordinator import HouseholdForecastCoordinator


class HouseholdForecastSensor(SensorEntity):
    """Expose the household load forecast value."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({ATTR_FORECASTS})

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
            key="load_forecast",
            translation_key="load_forecast",
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
    def native_value(self) -> float:
        """Return the current load forecast in kW."""
        return round(self._coordinator.load_forecast_kw, 3)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose household Load forecast metadata for dashboards and automations."""
        household_load_forecast_w = self._coordinator.household_load_forecast_w
        last_sample_kw = self._coordinator.last_sample_kw
        return {
            ATTR_FORECASTS: self._coordinator.forecast_slots,
            "household_load_forecast_w": (
                round(household_load_forecast_w, 1)
                if household_load_forecast_w is not None
                else None
            ),
            # Backward-compatible attribute alias.
            "household_base_load_w": (
                round(household_load_forecast_w, 1)
                if household_load_forecast_w is not None
                else None
            ),
            "learning_nights": self._coordinator.learning_nights,
            "data_since": self._coordinator.data_since,
            "last_sample_date": self._coordinator.last_sample_date,
            "last_sample_kw": (
                round(last_sample_kw, 3) if last_sample_kw is not None else None
            ),
            "reason": self._coordinator.reason,
        }


LoadForecastSensor = HouseholdForecastSensor
BaseLoadSensor = HouseholdForecastSensor
