"""Tests for Energy Advisor diagnostic sensors."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.energyadvisor.const import ATTR_FORECASTS
from custom_components.energyadvisor.sensor.diagnosticsensors import (
    BaseLoadSensor,
    BatteryFloorPercentSensor,
    BatteryFloorSensor,
    BatterySocForecastSensor,
    LearningNightsSensor,
    SellSafetyMarginSensor,
    StrategySensor,
)


UTC = timezone.utc


@pytest.fixture
def hass():
    h = MagicMock()
    h.config = MagicMock()
    h.config.time_zone = "UTC"
    h.states = MagicMock()
    return h


@pytest.fixture
def entry():
    e = MagicMock()
    e.entry_id = "test-entry"
    e.options = {}
    return e


@pytest.fixture
def device_info():
    return MagicMock()


def _make_battery_sensor(**overrides):
    sensor = SimpleNamespace(
        household_base_load_w=1234.56,
        learning_nights=7,
        solar_dominant=True,
        battery_floor_kwh=2.3456,
        battery_floor_pct=23.4,
        sell_safety_margin_kwh=1.23456,
        battery_soc_forecast=[
            {"end": "2024-01-01T10:00:00+00:00", "soc_pct": 80.0},
            {"end": "2024-01-01T11:00:00+00:00", "soc_pct": 72.5},
        ],
        async_add_update_listener=MagicMock(return_value=lambda: None),
    )
    for key, value in overrides.items():
        setattr(sensor, key, value)
    return sensor


@pytest.mark.asyncio
async def test_base_load_sensor_registers_listener_and_reports_value(
    hass, entry, device_info
):
    battery_sensor = _make_battery_sensor()
    sensor = BaseLoadSensor(entry, device_info, battery_sensor)
    sensor.hass = hass

    await sensor.async_added_to_hass()

    battery_sensor.async_add_update_listener.assert_called_once_with(
        sensor._on_battery_update
    )
    assert sensor.native_value == 1234.6
    assert sensor.extra_state_attributes == {
        "learning_nights": 7,
        "max_learning_nights": 30,
    }


@pytest.mark.parametrize(
    "solar_dominant, expected",
    [(True, "solar_aware"), (False, "price_arbitrage")],
)
def test_strategy_sensor_tracks_solar_dominance(entry, device_info, solar_dominant, expected):
    battery_sensor = _make_battery_sensor(solar_dominant=solar_dominant)
    sensor = StrategySensor(entry, device_info, battery_sensor)

    assert sensor.native_value == expected


def test_learning_nights_and_sell_margin_sensors_expose_live_values(
    entry, device_info
):
    battery_sensor = _make_battery_sensor()

    learning_sensor = LearningNightsSensor(entry, device_info, battery_sensor)
    margin_sensor = SellSafetyMarginSensor(entry, device_info, battery_sensor)

    assert learning_sensor.native_value == 7
    assert margin_sensor.native_value == 1.235


@pytest.mark.asyncio
async def test_battery_floor_sensor_restores_and_prefers_live_value(
    hass, entry, device_info
):
    battery_sensor = _make_battery_sensor(battery_floor_kwh=0.0)
    sensor = BatteryFloorSensor(entry, device_info, battery_sensor)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value="4.321")
    )

    await sensor.async_added_to_hass()
    assert sensor.native_value == 4.321

    battery_sensor.battery_floor_kwh = 2.34567
    assert sensor.native_value == 2.346


@pytest.mark.asyncio
async def test_battery_floor_percent_sensor_restores_and_prefers_live_value(
    hass, entry, device_info
):
    battery_sensor = _make_battery_sensor(battery_floor_pct=None)
    sensor = BatteryFloorPercentSensor(entry, device_info, battery_sensor)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value=41.25)
    )

    await sensor.async_added_to_hass()
    assert sensor.native_value == 41.25

    battery_sensor.battery_floor_pct = 18.5
    assert sensor.native_value == 18.5


@pytest.mark.asyncio
async def test_battery_soc_forecast_sensor_restores_and_reports_minimum(
    hass, entry, device_info
):
    battery_sensor = _make_battery_sensor(battery_soc_forecast=[])
    sensor = BatterySocForecastSensor(entry, device_info, battery_sensor)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(
        return_value=SimpleNamespace(native_value="55.5")
    )

    await sensor.async_added_to_hass()
    assert sensor.native_value == 55.5
    assert sensor.extra_state_attributes == {
        ATTR_FORECASTS: [],
        "min_soc_pct": None,
        "min_soc_time": None,
    }

    battery_sensor.battery_soc_forecast = [
        {"end": "2024-01-01T10:00:00+00:00", "soc_pct": 80.0},
        {"end": "2024-01-01T11:00:00+00:00", "soc_pct": 72.5},
    ]

    assert sensor.native_value == 80.0
    assert sensor.extra_state_attributes == {
        ATTR_FORECASTS: battery_sensor.battery_soc_forecast,
        "min_soc_pct": 72.5,
        "min_soc_time": "2024-01-01T11:00:00+00:00",
    }
