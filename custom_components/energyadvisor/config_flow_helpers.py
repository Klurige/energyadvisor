"""Shared helpers for the Energy Advisor config flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_BATHROOM_HUMIDITY_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_DEHUMIDIFIER_POWER_ENTITY,
    CONF_DEHUMIDIFIER_POWER_W,
    CONF_ELECTRICITY_VAT,
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_GRID_ENERGY_TAX,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_FIXED_CREDIT,
    CONF_GRID_FIXED_FEE,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_NOTE,
    CONF_GRID_VARIABLE_CREDIT,
    CONF_GRID_VARIABLE_FEE,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_POOL_PUMP_POWER_ENTITY,
    CONF_POOL_PUMP_POWER_W,
    CONF_POWER_ENTITY,
    CONF_POWER_METER_CONSUMPTION,
    CONF_SUPPLIER_FIXED_CREDIT,
    CONF_SUPPLIER_FIXED_FEE,
    CONF_SUPPLIER_NOTE,
    CONF_SUPPLIER_VARIABLE_CREDIT,
    CONF_SUPPLIER_VARIABLE_FEE,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_WATER_HEATER_MAX_HOURS,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
    DEV_DEFAULTS,
    DEV_DEFAULTS_ENABLED,
    parse_unit_of_measurement,
)

_LOGGER = logging.getLogger(__name__)

LEGACY_DEV_DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    CONF_WATER_HEATER_POWER_ENTITY: ("water_heater_entity",),
    CONF_WATER_HEATER_MAX_HOURS: ("water_heater_min_hours",),
    CONF_POOL_PUMP_POWER_ENTITY: ("pool_pump_entity",),
    CONF_DEHUMIDIFIER_POWER_ENTITY: ("dehumidifier_entity",),
}

BATTERY_STEP_ENTITY_KEYS: tuple[str, ...] = (
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
)
BATTERY_STEP_NUMERIC_KEYS: tuple[str, ...] = (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_DEGRADATION_COST,
)
GRID_METERING_ENTITY_KEYS: tuple[str, ...] = (
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
)
HOUSEHOLD_SENSOR_ENTITY_KEYS: tuple[str, ...] = (
    CONF_POWER_METER_CONSUMPTION,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
)
HOUSEHOLD_BINARY_ENTITY_KEYS: tuple[str, ...] = (
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
)
HOT_WATER_ENTITY_KEYS: tuple[str, ...] = (
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_BATHROOM_HUMIDITY_ENTITY,
)
HOT_WATER_NUMERIC_KEYS: tuple[str, ...] = (
    CONF_WATER_HEATER_POWER_W,
    CONF_WATER_HEATER_MAX_HOURS,
)
FLEXIBLE_LOADS_ENTITY_KEYS: tuple[str, ...] = (
    CONF_POOL_PUMP_POWER_ENTITY,
    CONF_DEHUMIDIFIER_POWER_ENTITY,
)
FLEXIBLE_LOADS_NUMERIC_KEYS: tuple[str, ...] = (
    CONF_POOL_PUMP_POWER_W,
    CONF_DEHUMIDIFIER_POWER_W,
)

ALL_OPTIMIZER_ENTITY_KEYS: tuple[str, ...] = (
    *BATTERY_STEP_ENTITY_KEYS,
    *GRID_METERING_ENTITY_KEYS,
    *HOUSEHOLD_SENSOR_ENTITY_KEYS,
    *HOUSEHOLD_BINARY_ENTITY_KEYS,
    *HOT_WATER_ENTITY_KEYS,
    *FLEXIBLE_LOADS_ENTITY_KEYS,
)
ALL_OPTIMIZER_NUMERIC_KEYS: tuple[str, ...] = (
    *HOT_WATER_NUMERIC_KEYS,
    *FLEXIBLE_LOADS_NUMERIC_KEYS,
)


def _parse_unit_of_measurement(unit_str: str) -> tuple[str | None, str | None]:
    """Delegate to shared implementation in const.py."""
    return parse_unit_of_measurement(unit_str)


def _dev_default(*keys: str):
    """Return the first matching dev default when DEV_DEFAULTS_ENABLED."""
    if not DEV_DEFAULTS_ENABLED:
        return None
    for key in keys:
        if key in DEV_DEFAULTS:
            return DEV_DEFAULTS[key]
    return None


def _schema_default(value: Any) -> Any:
    """Return a voluptuous default marker for an optional field."""
    return value if value is not None else vol.UNDEFINED


def _form_value(data: dict[str, Any], key: str) -> Any:
    """Return stored value or matching dev default for a config key."""
    value = data.get(key)
    if value is not None:
        return value
    return _dev_default(key, *LEGACY_DEV_DEFAULT_ALIASES.get(key, ()))


def _validate_optional_sensor_entities(
    hass: HomeAssistant,
    entity_ids: dict[str, str | None],
) -> dict[str, str]:
    """Validate optional sensor entity ids when provided."""
    errors: dict[str, str] = {}
    for key, entity_id in entity_ids.items():
        if entity_id and hass.states.get(entity_id) is None:
            errors[key] = "entity_not_found"
    return errors


def _build_solar_forecast_schema(values: dict[str, Any]) -> dict[Any, Any]:
    """Build the config schema for optional solar forecast inputs."""
    return {
        vol.Optional(
            CONF_FORECAST_ENTITY,
            default=_schema_default(values.get(CONF_FORECAST_ENTITY)),
            description={"suggested_value": values.get(CONF_FORECAST_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_POWER_ENTITY,
            default=_schema_default(values.get(CONF_POWER_ENTITY)),
            description={"suggested_value": values.get(CONF_POWER_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_FORECAST_TOMORROW_ENTITY,
            default=_schema_default(values.get(CONF_FORECAST_TOMORROW_ENTITY)),
            description={"suggested_value": values.get(CONF_FORECAST_TOMORROW_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
    }


def _build_battery_schema(
    values: dict[str, Any], unit_of_measurement: str
) -> dict[Any, Any]:
    """Build the config schema for battery hardware inputs."""
    return {
        vol.Optional(
            CONF_BATTERY_CAPACITY_KWH,
            default=_schema_default(values.get(CONF_BATTERY_CAPACITY_KWH)),
            description={"suffix": "kWh"},
        ): vol.All(vol.Coerce(float), vol.Range(min=0.001)),
        vol.Optional(
            CONF_BATTERY_MAX_CHARGE_POWER_W,
            default=_schema_default(values.get(CONF_BATTERY_MAX_CHARGE_POWER_W)),
            description={"suffix": "W"},
        ): vol.All(vol.Coerce(float), vol.Range(min=1)),
        vol.Optional(
            CONF_BATTERY_MAX_DISCHARGE_POWER_W,
            default=_schema_default(values.get(CONF_BATTERY_MAX_DISCHARGE_POWER_W)),
            description={"suffix": "W"},
        ): vol.All(vol.Coerce(float), vol.Range(min=1)),
        vol.Optional(
            CONF_BATTERY_DEGRADATION_COST,
            default=_schema_default(values.get(CONF_BATTERY_DEGRADATION_COST)),
            description={"suffix": unit_of_measurement},
        ): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(
            CONF_BATTERY_SOC_ENTITY,
            default=_schema_default(values.get(CONF_BATTERY_SOC_ENTITY)),
            description={"suggested_value": values.get(CONF_BATTERY_SOC_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_BATTERY_CHARGE_POWER_ENTITY,
            default=_schema_default(values.get(CONF_BATTERY_CHARGE_POWER_ENTITY)),
            description={
                "suggested_value": values.get(CONF_BATTERY_CHARGE_POWER_ENTITY)
            },
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
    }


def _build_grid_metering_schema(values: dict[str, Any]) -> dict[Any, Any]:
    """Build the config schema for grid metering inputs."""
    return {
        vol.Optional(
            CONF_GRID_IMPORT_ENTITY,
            default=_schema_default(values.get(CONF_GRID_IMPORT_ENTITY)),
            description={"suggested_value": values.get(CONF_GRID_IMPORT_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_GRID_EXPORT_ENTITY,
            default=_schema_default(values.get(CONF_GRID_EXPORT_ENTITY)),
            description={"suggested_value": values.get(CONF_GRID_EXPORT_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
    }


def _build_household_schema(values: dict[str, Any]) -> dict[Any, Any]:
    """Build the config schema for household load inputs."""
    return {
        vol.Optional(
            CONF_POWER_METER_CONSUMPTION,
            default=_schema_default(values.get(CONF_POWER_METER_CONSUMPTION)),
            description={"suggested_value": values.get(CONF_POWER_METER_CONSUMPTION)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_OUTDOOR_TEMPERATURE_ENTITY,
            default=_schema_default(values.get(CONF_OUTDOOR_TEMPERATURE_ENTITY)),
            description={"suggested_value": values.get(CONF_OUTDOOR_TEMPERATURE_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_WATER_HEATER_ACTIVE_ENTITY,
            default=_schema_default(values.get(CONF_WATER_HEATER_ACTIVE_ENTITY)),
            description={
                "suggested_value": values.get(CONF_WATER_HEATER_ACTIVE_ENTITY)
            },
        ): EntitySelector(
            EntitySelectorConfig(
                domain=["binary_sensor", "input_boolean", "sensor", "switch"]
            )
        ),
        vol.Optional(
            CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
            default=_schema_default(values.get(CONF_CENTRAL_HEATING_ACTIVE_ENTITY)),
            description={
                "suggested_value": values.get(CONF_CENTRAL_HEATING_ACTIVE_ENTITY)
            },
        ): EntitySelector(
            EntitySelectorConfig(
                domain=["binary_sensor", "input_boolean", "sensor", "switch"]
            )
        ),
    }


def _build_hot_water_schema(values: dict[str, Any]) -> dict[Any, Any]:
    """Build the config schema for hot-water planner inputs."""
    return {
        vol.Optional(
            CONF_WATER_HEATER_POWER_ENTITY,
            default=_schema_default(values.get(CONF_WATER_HEATER_POWER_ENTITY)),
            description={"suggested_value": values.get(CONF_WATER_HEATER_POWER_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_WATER_HEATER_POWER_W,
            default=_schema_default(values.get(CONF_WATER_HEATER_POWER_W)),
            description={"suffix": "W"},
        ): vol.All(vol.Coerce(float), vol.Range(min=1)),
        vol.Optional(
            CONF_WATER_HEATER_MAX_HOURS,
            default=_schema_default(values.get(CONF_WATER_HEATER_MAX_HOURS)),
            description={"suffix": "h"},
        ): vol.All(vol.Coerce(float), vol.Range(min=0.001)),
        vol.Optional(
            CONF_BATHROOM_HUMIDITY_ENTITY,
            default=_schema_default(values.get(CONF_BATHROOM_HUMIDITY_ENTITY)),
            description={"suggested_value": values.get(CONF_BATHROOM_HUMIDITY_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
    }


def _build_flexible_loads_schema(values: dict[str, Any]) -> dict[Any, Any]:
    """Build the config schema for flexible-load inputs."""
    return {
        vol.Optional(
            CONF_POOL_PUMP_POWER_ENTITY,
            default=_schema_default(values.get(CONF_POOL_PUMP_POWER_ENTITY)),
            description={"suggested_value": values.get(CONF_POOL_PUMP_POWER_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_POOL_PUMP_POWER_W,
            default=_schema_default(values.get(CONF_POOL_PUMP_POWER_W)),
            description={"suffix": "W"},
        ): vol.All(vol.Coerce(float), vol.Range(min=1)),
        vol.Optional(
            CONF_DEHUMIDIFIER_POWER_ENTITY,
            default=_schema_default(values.get(CONF_DEHUMIDIFIER_POWER_ENTITY)),
            description={"suggested_value": values.get(CONF_DEHUMIDIFIER_POWER_ENTITY)},
        ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        vol.Optional(
            CONF_DEHUMIDIFIER_POWER_W,
            default=_schema_default(values.get(CONF_DEHUMIDIFIER_POWER_W)),
            description={"suffix": "W"},
        ): vol.All(vol.Coerce(float), vol.Range(min=1)),
    }


def _validate_solar_forecast_entities(
    hass: HomeAssistant,
    forecast_entity: str | None,
    power_entity: str | None,
    tomorrow_entity: str | None,
) -> dict[str, str]:
    """Validate the optional solar forecast entity configuration."""
    errors: dict[str, str] = {}

    if bool(forecast_entity) != bool(power_entity):
        if not forecast_entity:
            errors[CONF_FORECAST_ENTITY] = "solar_entity_required"
        else:
            errors[CONF_POWER_ENTITY] = "solar_entity_required"
        return errors

    for key, entity_id in (
        (CONF_FORECAST_ENTITY, forecast_entity),
        (CONF_POWER_ENTITY, power_entity),
        (CONF_FORECAST_TOMORROW_ENTITY, tomorrow_entity),
    ):
        if entity_id and hass.states.get(entity_id) is None:
            errors[key] = "entity_not_found"

    return errors


def _validate_battery_settings(
    battery_capacity_kwh: float | None,
    battery_max_charge_power_w: float | None,
) -> dict[str, str]:
    """Validate the optional battery configuration."""
    errors: dict[str, str] = {}
    has_capacity = battery_capacity_kwh is not None
    has_power = battery_max_charge_power_w is not None

    if has_capacity != has_power:
        if not has_capacity:
            errors[CONF_BATTERY_CAPACITY_KWH] = "battery_setting_required"
        else:
            errors[CONF_BATTERY_MAX_CHARGE_POWER_W] = "battery_setting_required"

    return errors


async def _validate_nordpool_prices_sensor(
    hass: HomeAssistant, entity_id: str
) -> tuple[bool, dict | None]:
    """Validate the Nordpool prices sensor by checking if it exists and is available."""
    if not entity_id:
        return False, None

    state = hass.states.get(entity_id)

    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        _LOGGER.warning(
            f"Nordpool prices sensor '{entity_id}' not found or unavailable."
        )
        return False, None

    unit_of_measurement = state.attributes.get("unit_of_measurement", "")
    currency_from_attributes = state.attributes.get("currency", "")

    # Try to parse currency from unit_of_measurement
    parsed_currency, parsed_unit = _parse_unit_of_measurement(unit_of_measurement)

    # Use parsed currency, or fallback to direct attribute, or use a default
    final_currency = parsed_currency or currency_from_attributes or "EUR"
    final_unit = parsed_unit or "MWh"

    _LOGGER.debug(
        f"Extracted from sensor '{entity_id}': "
        f"unit_of_measurement='{unit_of_measurement}', "
        f"parsed_currency='{parsed_currency}', "
        f"currency_attribute='{currency_from_attributes}', "
        f"final_currency='{final_currency}', "
        f"final_unit='{final_unit}'"
    )

    attributes = {
        "unit_of_measurement": unit_of_measurement,
        "currency": final_currency,
        "energy_unit": final_unit,
        "price_divisor": 100 if state.attributes.get("prices_in_cents", False) else 1,
    }
    return True, attributes
