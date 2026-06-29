"""Diagnostic sensors for the Energy Advisor battery optimiser.

These sensors expose internal state of the BatteryChargeModeSensor so that
behaviour can be monitored and analysed directly in dashboards without
requiring template sensors.

Sensors
-------
- base_load            House base load in Watts learned from quiet nights.
- strategy             Which daily strategy is active: solar_aware / price_arbitrage.
- battery_floor        Energy (kWh) that must stay in the battery until solar starts.
- learning_nights      Number of quiet nights used in the rolling base-load average.
- battery_soc_forecast Forecasted battery SoC% over the planned charge schedule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from ..const import (
    ATTR_FORECASTS,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

if TYPE_CHECKING:
    from .batterychargemodesensor import BatteryChargeModeSensor

_LOGGER = logging.getLogger(__name__)


class _DiagnosticBase(SensorEntity):
    """Shared scaffolding for all Energy Advisor diagnostic sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
        description: SensorEntityDescription,
    ) -> None:
        self._battery_sensor = battery_sensor
        self.entity_description = description
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[description.key]
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = build_sensor_unique_id(entry, description.key)
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, False
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._battery_sensor.async_add_update_listener(self._on_battery_update)
        )

    def _on_battery_update(self) -> None:
        self.async_write_ha_state()


class BaseLoadSensor(_DiagnosticBase):
    """Reports the learned household base load in Watts."""

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
    ) -> None:
        super().__init__(
            entry,
            device_info,
            battery_sensor,
            SensorEntityDescription(
                key="base_load",
                translation_key="base_load",
                native_unit_of_measurement="W",
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )

    @property
    def native_value(self) -> float | None:
        value = self._battery_sensor.household_base_load_w
        return round(value, 1) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "learning_nights": self._battery_sensor.learning_nights,
            "max_learning_nights": 30,
        }


class StrategySensor(_DiagnosticBase):
    """Reports which daily battery strategy is currently active."""

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
    ) -> None:
        super().__init__(
            entry,
            device_info,
            battery_sensor,
            SensorEntityDescription(
                key="strategy",
                translation_key="strategy",
            ),
        )

    @property
    def native_value(self) -> str:
        return "solar_aware" if self._battery_sensor.solar_dominant else "price_arbitrage"


class BatteryFloorSensor(_DiagnosticBase):
    """Reports how much energy must stay in the battery until solar starts."""

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
    ) -> None:
        super().__init__(
            entry,
            device_info,
            battery_sensor,
            SensorEntityDescription(
                key="battery_floor",
                translation_key="battery_floor",
                native_unit_of_measurement="kWh",
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )
        # Always record the state so dashboard charts load without the
        # "Loading" spinner. The state value (kWh) is small and useful for
        # historical analysis regardless of the global exclude_from_recording
        # integration option.
        self._attr_exclude_from_recording = False

    @property
    def native_value(self) -> float:
        return round(self._battery_sensor.battery_floor_kwh, 3)


class LearningNightsSensor(_DiagnosticBase):
    """Reports how many quiet nights are in the rolling base-load average."""

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
    ) -> None:
        super().__init__(
            entry,
            device_info,
            battery_sensor,
            SensorEntityDescription(
                key="learning_nights",
                translation_key="learning_nights",
                native_unit_of_measurement="nights",
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )

    @property
    def native_value(self) -> int:
        return self._battery_sensor.learning_nights


class BatterySocForecastSensor(_DiagnosticBase):
    """Forecasted battery SoC% over the planned charge schedule.

    State      : forecasted SoC% for the current 15-min slot (= actual SoC at
                 recompute time; drifts between recomputes as time passes).
    Attributes :
        forecasts    – list of {end: str, soc_pct: float} covering the full
                       planned schedule (same time horizon as charge_entries).
        min_soc_pct  – lowest SoC% expected anywhere in the forecast window.
        min_soc_time – ISO timestamp when the minimum SoC is reached.
    """

    _unrecorded_attributes = frozenset({ATTR_FORECASTS})

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        battery_sensor: "BatteryChargeModeSensor",
    ) -> None:
        super().__init__(
            entry,
            device_info,
            battery_sensor,
            SensorEntityDescription(
                key="battery_soc_forecast",
                translation_key="battery_soc_forecast",
                native_unit_of_measurement="%",
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )
        # Always record the state (a single % value) so dashboard charts load
        # without the "Loading" spinner. The large forecasts attribute is
        # excluded from recording via _unrecorded_attributes.
        self._attr_exclude_from_recording = False

    @property
    def native_value(self) -> float | None:
        forecast = self._battery_sensor.battery_soc_forecast
        return forecast[0]["soc_pct"] if forecast else None

    @property
    def extra_state_attributes(self) -> dict:
        forecast = self._battery_sensor.battery_soc_forecast
        if not forecast:
            return {ATTR_FORECASTS: [], "min_soc_pct": None, "min_soc_time": None}
        min_entry = min(forecast, key=lambda e: e["soc_pct"])
        return {
            ATTR_FORECASTS: forecast,
            "min_soc_pct": min_entry["soc_pct"],
            "min_soc_time": min_entry["end"],
        }
