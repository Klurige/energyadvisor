"""Heating forecast sensor."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    ATTR_FORECASTS,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..coordinators.heating_forecast_coordinator import HeatingForecastCoordinator


class HeatingForecastSensor(SensorEntity):
    """Expose the forecast heating power and its 48-hour slot contract."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({ATTR_FORECASTS, "model"})

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        coordinator: HeatingForecastCoordinator,
    ) -> None:
        self._coordinator = coordinator
        description = SensorEntityDescription(
            key="heating_forecast",
            translation_key="heating_forecast",
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

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_update_callback(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the forecast heating power for the current slot in kW."""
        return self._coordinator.current_heating_kw

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the slot forecast, daily totals and model diagnostics."""
        return {
            ATTR_FORECASTS: self._coordinator.forecast_slots,
            "energy_today_kwh": self._coordinator.energy_for_local_date_kwh(0),
            "energy_tomorrow_kwh": self._coordinator.energy_for_local_date_kwh(1),
            "quality_status": self._coordinator.quality_status,
            "quality_warnings": self._coordinator.quality_warnings,
            "reason": self._coordinator.reason,
            **self._coordinator.diagnostics,
        }
