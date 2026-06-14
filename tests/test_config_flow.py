"""Tests for config and options flow behavior."""

from unittest.mock import MagicMock

import pytest

from custom_components.electricitypricelevels.config_flow import (
    ElectricityPriceLevelFlowHandler,
    ElectricityPriceLevelOptionFlowHandler,
    _parse_unit_of_measurement,
    _validate_nordpool_prices_sensor,
)
from custom_components.electricitypricelevels.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_POWER_ENTITY,
)


def _make_state(
    state_value: str = "1.23",
    attributes: dict | None = None,
) -> MagicMock:
    """Build a simple Home Assistant state mock."""
    state = MagicMock()
    state.state = state_value
    state.attributes = attributes or {}
    return state


# --- Tests for _parse_unit_of_measurement ---


@pytest.mark.parametrize(
    "unit_str, expected",
    [
        ("SEK/kWh", ("SEK", "kWh")),
        ("EUR/MWh", ("EUR", "MWh")),
        ("SEK/kW", ("SEK", "kW")),
        ("EUR", ("EUR", None)),
        ("kWh", (None, "kWh")),
        ("MWh", (None, "MWh")),
        ("", (None, None)),
        (None, (None, None)),
        ("  SEK / kWh  ", ("SEK", "kWh")),
        ("/", (None, None)),
        ("a/b/c", (None, None)),
        ("NOK/Wh", ("NOK", "Wh")),
    ],
    ids=[
        "currency_slash_energy",
        "eur_slash_mwh",
        "currency_slash_kw",
        "currency_only",
        "energy_only_kwh",
        "energy_only_mwh",
        "empty_string",
        "none_input",
        "whitespace_around_parts",
        "slash_only",
        "triple_slash",
        "nok_slash_wh",
    ],
)
def test_parse_unit_of_measurement(unit_str, expected):
    """Test _parse_unit_of_measurement parses various formats correctly."""
    assert _parse_unit_of_measurement(unit_str) == expected


# --- Tests for _validate_nordpool_prices_sensor ---


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_valid():
    """Test validation succeeds for a valid sensor."""
    hass = MagicMock()
    state = MagicMock()
    state.state = "1.23"
    state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "currency": "SEK",
    }
    hass.states.get.return_value = state

    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "sensor.nordpool")

    assert is_valid is True
    assert attrs["currency"] == "SEK"
    assert attrs["energy_unit"] == "kWh"


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_empty_entity_id():
    """Test validation fails for empty entity id."""
    hass = MagicMock()
    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "")
    assert is_valid is False
    assert attrs is None


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_not_found():
    """Test validation fails when sensor entity does not exist."""
    hass = MagicMock()
    hass.states.get.return_value = None

    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "sensor.nonexistent")

    assert is_valid is False
    assert attrs is None


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_unavailable():
    """Test validation fails when sensor is unavailable."""
    hass = MagicMock()
    state = MagicMock()
    state.state = "unavailable"
    hass.states.get.return_value = state

    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "sensor.nordpool")

    assert is_valid is False
    assert attrs is None


@pytest.mark.asyncio
async def test_validate_nordpool_prices_sensor_defaults():
    """Test validation returns defaults when sensor has no unit attributes."""
    hass = MagicMock()
    state = MagicMock()
    state.state = "42.0"
    state.attributes = {}
    hass.states.get.return_value = state

    is_valid, attrs = await _validate_nordpool_prices_sensor(hass, "sensor.nordpool")

    assert is_valid is True
    assert attrs["currency"] == "EUR"
    assert attrs["energy_unit"] == "MWh"


# --- Original tests ---


@pytest.mark.asyncio
async def test_options_flow_contains_solar_and_battery_fields() -> None:
    """Test options flow includes solar and battery configuration fields."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry
    hass = MagicMock()
    hass.states.get.return_value = None
    handler.hass = hass

    result = await handler.async_step_init()
    schema = result["data_schema"].schema
    schema_keys = [getattr(k, "schema", k) for k in schema]

    assert CONF_FORECAST_ENTITY in schema_keys
    assert CONF_POWER_ENTITY in schema_keys
    assert CONF_FORECAST_TOMORROW_ENTITY in schema_keys
    assert CONF_BATTERY_CAPACITY_KWH in schema_keys
    assert CONF_BATTERY_MAX_CHARGE_POWER_W in schema_keys
    assert CONF_BATTERY_DEGRADATION_COST in schema_keys


@pytest.mark.asyncio
async def test_options_flow_threshold_validation_error() -> None:
    """Test options flow returns validation error when low >= high."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry
    state = MagicMock()
    state.state = "1.23"
    state.attributes = {
        "unit_of_measurement": "EUR/kWh",
        "currency": "EUR",
    }
    hass = MagicMock()
    hass.states.get.return_value = state
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
            CONF_LOW_THRESHOLD: 2.0,
            CONF_HIGH_THRESHOLD: 1.0,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "low_threshold_higher_than_high_threshold"


@pytest.mark.asyncio
async def test_main_flow_thresholds_proceeds_to_solar_forecast() -> None:
    """Test thresholds step proceeds to solar_forecast step."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.hass = MagicMock()
    handler.data = {
        CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
    }

    result = await handler.async_step_thresholds(
        {
            CONF_LOW_THRESHOLD: 0.10,
            CONF_HIGH_THRESHOLD: 0.20,
        }
    )

    assert result["type"] == "form"
    assert result["step_id"] == "solar_forecast"


@pytest.mark.asyncio
async def test_main_flow_solar_forecast_rejects_missing_tomorrow_entity() -> None:
    """Test solar step validates the optional tomorrow forecast entity."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.data = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.solar_today": _make_state("500", {"watts": {}}),
        "sensor.inverter_power": _make_state("1500"),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_solar_forecast(
        {
            CONF_FORECAST_ENTITY: "sensor.solar_today",
            CONF_POWER_ENTITY: "sensor.inverter_power",
            CONF_FORECAST_TOMORROW_ENTITY: "sensor.solar_tomorrow",
        }
    )

    assert result["type"] == "form"
    assert result["errors"][CONF_FORECAST_TOMORROW_ENTITY] == "entity_not_found"


@pytest.mark.asyncio
async def test_main_flow_valid_solar_forecast_proceeds_to_battery() -> None:
    """Test valid solar settings proceed to the battery step."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.data = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.solar_today": _make_state("500", {"watts": {}}),
        "sensor.inverter_power": _make_state("1500"),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_solar_forecast(
        {
            CONF_FORECAST_ENTITY: "sensor.solar_today",
            CONF_POWER_ENTITY: "sensor.inverter_power",
        }
    )

    assert result["type"] == "form"
    assert result["step_id"] == "battery"


@pytest.mark.asyncio
async def test_main_flow_battery_requires_capacity_and_power_together() -> None:
    """Test battery step requires capacity and max power together."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.data = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}
    handler.hass = MagicMock()

    result = await handler.async_step_battery(
        {
            CONF_BATTERY_CAPACITY_KWH: 10.0,
        }
    )

    assert result["type"] == "form"
    assert (
        result["errors"][CONF_BATTERY_MAX_CHARGE_POWER_W] == "battery_setting_required"
    )


@pytest.mark.asyncio
async def test_main_flow_battery_step_creates_entry_and_preserves_zero_margin() -> None:
    """Test battery step creates the entry and keeps a zero degradation margin."""
    handler = ElectricityPriceLevelFlowHandler()
    handler.data = {
        CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
        "unit_of_measurement": "EUR/kWh",
        "currency": "EUR",
        "energy_unit": "kWh",
        "price_divisor": 1,
    }
    handler.hass = MagicMock()

    result = await handler.async_step_battery(
        {
            CONF_BATTERY_CAPACITY_KWH: 10.0,
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000.0,
            CONF_BATTERY_DEGRADATION_COST: 0.0,
        }
    )

    assert result["type"] == "create_entry"
    assert result["options"][CONF_BATTERY_CAPACITY_KWH] == 10.0
    assert result["options"][CONF_BATTERY_MAX_CHARGE_POWER_W] == 5000.0
    assert result["options"][CONF_BATTERY_DEGRADATION_COST] == 0.0


@pytest.mark.asyncio
async def test_options_flow_requires_solar_power_pair() -> None:
    """Test options flow requires forecast and power entities together."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.nordpool_prices": _make_state(
            attributes={
                "unit_of_measurement": "EUR/kWh",
                "currency": "EUR",
            }
        ),
        "sensor.solar_today": _make_state("500"),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
            CONF_FORECAST_ENTITY: "sensor.solar_today",
        }
    )

    assert result["type"] == "form"
    assert result["errors"][CONF_POWER_ENTITY] == "solar_entity_required"


@pytest.mark.asyncio
async def test_options_flow_rejects_missing_tomorrow_entity() -> None:
    """Test options flow validates the optional tomorrow forecast entity."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.nordpool_prices": _make_state(
            attributes={
                "unit_of_measurement": "EUR/kWh",
                "currency": "EUR",
            }
        ),
        "sensor.solar_today": _make_state("500"),
        "sensor.inverter_power": _make_state("1500"),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
            CONF_FORECAST_ENTITY: "sensor.solar_today",
            CONF_POWER_ENTITY: "sensor.inverter_power",
            CONF_FORECAST_TOMORROW_ENTITY: "sensor.solar_tomorrow",
        }
    )

    assert result["type"] == "form"
    assert result["errors"][CONF_FORECAST_TOMORROW_ENTITY] == "entity_not_found"


@pytest.mark.asyncio
async def test_options_flow_requires_battery_capacity_and_power_together() -> None:
    """Test options flow requires battery capacity and max power together."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.nordpool_prices": _make_state(
            attributes={
                "unit_of_measurement": "EUR/kWh",
                "currency": "EUR",
            }
        ),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
            CONF_BATTERY_CAPACITY_KWH: 10.0,
        }
    )

    assert result["type"] == "form"
    assert (
        result["errors"][CONF_BATTERY_MAX_CHARGE_POWER_W] == "battery_setting_required"
    )


@pytest.mark.asyncio
async def test_options_flow_preserves_zero_battery_margin() -> None:
    """Test options flow keeps a zero degradation margin instead of clearing it."""
    config_entry = MagicMock()
    config_entry.options = {CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices"}

    handler = ElectricityPriceLevelOptionFlowHandler()
    handler._config_entry = config_entry

    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.nordpool_prices": _make_state(
            attributes={
                "unit_of_measurement": "EUR/kWh",
                "currency": "EUR",
            }
        ),
    }.get(entity_id)
    handler.hass = hass

    result = await handler.async_step_init(
        {
            CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
            CONF_BATTERY_CAPACITY_KWH: 10.0,
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000.0,
            CONF_BATTERY_DEGRADATION_COST: 0.0,
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_BATTERY_DEGRADATION_COST] == 0.0
