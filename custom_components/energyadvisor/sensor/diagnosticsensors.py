"""Diagnostic sensors for the Energy Advisor battery optimiser.

These sensors expose internal state of the BatteryChargeModeSensor so that
behaviour can be monitored and analysed directly in dashboards without
requiring template sensors.

Sensors
-------
- base_load            House base load in Watts learned from quiet nights.
- strategy             Which daily strategy is active: solar_aware / price_arbitrage.
- battery_floor        Energy (kWh) that must stay in the battery until solar starts.
- battery_floor_pct    Same floor expressed as % of battery capacity (for inverter automation).
- learning_nights      Number of quiet nights used in the rolling base-load average.
- sell_safety_margin   Learned sell safety margin (kWh) adjusted at each dawn.
- battery_soc_forecast Forecasted battery SoC% over the planned charge schedule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    RestoreSensor,
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
        return (
            "solar_aware" if self._battery_sensor.solar_dominant else "price_arbitrage"
        )


class BatteryFloorSensor(_DiagnosticBase, RestoreSensor):
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
        self._attr_exclude_from_recording = False
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float:
        live = self._battery_sensor.battery_floor_kwh
        if live:
            self._restored_value = None
            return round(live, 3)
        return (
            round(self._restored_value, 3) if self._restored_value is not None else 0.0
        )


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


class BatteryFloorPercentSensor(_DiagnosticBase, RestoreSensor):
    """Reports the battery floor as a percentage of capacity.

    This is the value to use as the inverter's 'force discharge to level'
    setting — it tells the inverter exactly how far it may discharge during
    sell mode while still protecting the battery for overnight load.
    """

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
                key="battery_floor_pct",
                translation_key="battery_floor_pct",
                native_unit_of_measurement="%",
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )
        self._attr_exclude_from_recording = False
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float | None:
        live = self._battery_sensor.battery_floor_pct
        if live is not None:
            self._restored_value = None
            return live
        return self._restored_value


class SellSafetyMarginSensor(_DiagnosticBase, RestoreSensor):
    """Reports the learned sell safety margin in kWh.

    This margin is added on top of the battery floor constraint. It
    self-calibrates each dawn: decreases when the battery has too much
    energy at sunrise (sell was too conservative), increases slightly
    when the battery was near the hardware 5% floor (risk of grid import).
    """

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
                key="sell_safety_margin",
                translation_key="sell_safety_margin",
                native_unit_of_measurement="kWh",
                state_class=SensorStateClass.MEASUREMENT,
            ),
        )
        self._attr_exclude_from_recording = False
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float | None:
        live = self._battery_sensor.sell_safety_margin_kwh
        self._restored_value = None
        return round(live, 3)


class BatterySocForecastSensor(_DiagnosticBase, RestoreSensor):
    """Forecasted battery SoC% over the planned charge schedule.

    State      : forecasted SoC% for the first future 15-min slot (anchored to
                 actual SoC at each recompute). Restored from storage on restart
                 so dashboard charts load immediately without "Loading" delay.
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
        self._attr_exclude_from_recording = False
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float | None:
        forecast = self._battery_sensor.battery_soc_forecast
        if forecast:
            self._restored_value = None
            return forecast[0]["soc_pct"]
        return self._restored_value

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
