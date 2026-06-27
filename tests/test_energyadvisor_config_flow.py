"""Focused config-flow tests for the Energy Advisor rename."""

from unittest.mock import MagicMock

import pytest

from custom_components.energyadvisor import config_flow as config_flow_module
from custom_components.energyadvisor.config_flow import (
    ElectricityPriceLevelFlowHandler,
    _validate_nordpool_prices_sensor,
)
from custom_components.energyadvisor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_NORDPOOL_PRICES_SENSOR,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_valid():
    """Energy Advisor should reuse the Nord Pool validation helper."""
    hass = MagicMock()
    state = MagicMock()
    state.state = "1.23"
    state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "currency": "SEK",
    }
    hass.states.get.return_value = state

    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "sensor.nordpool")

    assert DOMAIN == "energyadvisor"
    assert is_valid is True
    assert attrs["currency"] == "SEK"
    assert attrs["energy_unit"] == "kWh"


@pytest.mark.asyncio
async def test_main_flow_user_prefills_dev_default_prices_sensor(monkeypatch) -> None:
    """Energy Advisor add flow should prefill the Nord Pool entity from dev defaults."""
    monkeypatch.setattr(config_flow_module, "DEV_DEFAULTS_ENABLED", True)
    monkeypatch.setattr(
        config_flow_module,
        "DEV_DEFAULTS",
        {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"},
    )

    handler = ElectricityPriceLevelFlowHandler()
    handler.hass = MagicMock()

    result = await handler.async_step_user()

    assert result["type"] == "form"
    assert result["data_schema"]({})[CONF_NORDPOOL_PRICES_SENSOR] == (
        "sensor.nordpool_prices"
    )


@pytest.mark.asyncio
async def test_battery_step_creates_energy_advisor_entry_title():
    """The final config-flow step should create an Energy Advisor entry."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.hass = MagicMock()
    handler.hass.states.get.return_value = None
    handler.data = {
        CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
        "unit_of_measurement": "SEK/kWh",
        "currency": "SEK",
        "energy_unit": "kWh",
        "price_divisor": 1,
    }

    result = await handler.async_step_battery(
        {
            CONF_BATTERY_CAPACITY_KWH: None,
            CONF_BATTERY_MAX_CHARGE_POWER_W: None,
            CONF_BATTERY_DEGRADATION_COST: None,
        }
    )
    assert result["step_id"] == "grid_metering"

    result = await handler.async_step_grid_metering({})
    assert result["step_id"] == "household"

    result = await handler.async_step_household({})
    assert result["step_id"] == "hot_water"

    result = await handler.async_step_hot_water({})
    assert result["step_id"] == "flexible_loads"

    result = await handler.async_step_flexible_loads({})
    assert result["type"] == "create_entry"
    assert result["title"] == "Energy Advisor"
