"""Year-to-date qualifying battery exports."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import PREFERRED_SENSOR_ENTITY_IDS, build_sensor_unique_id
from ..coordinators.grid_resale_coordinator import GridResaleCoordinator
from ..grid_resale import STOCKHOLM


class GridResaleSensor(CoordinatorEntity[GridResaleCoordinator], SensorEntity):
    """Expose the current Swedish tax year's qualifying energy."""

    _attr_has_entity_name = True
    _attr_translation_key = "grid_resale"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, entry, device_info, coordinator: GridResaleCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS["grid_resale"]
        self._attr_unique_id = build_sensor_unique_id(entry, "grid_resale")
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.sources_configured

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data["eligible_export_kwh"] if data else None

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.data or {}

    @property
    def last_reset(self) -> datetime | None:
        data = self.coordinator.data
        return datetime(data["tax_year"], 1, 1, tzinfo=STOCKHOLM) if data else None
