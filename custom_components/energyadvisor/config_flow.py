"""Config flow for the Energy Advisor integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN

from .const import (
    CONF_BATHROOM_HUMIDITY_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DEHUMIDIFIER_POWER_ENTITY,
    CONF_DEHUMIDIFIER_POWER_W,
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_LOW_THRESHOLD,
    CONF_HIGH_THRESHOLD,
    CONF_GRID_EXPORT_ENTITY,
    CONF_SUPPLIER_NOTE,
    CONF_SUPPLIER_FIXED_FEE,
    CONF_SUPPLIER_VARIABLE_FEE,
    CONF_SUPPLIER_FIXED_CREDIT,
    CONF_SUPPLIER_VARIABLE_CREDIT,
    CONF_GRID_IMPORT_ENTITY,
    CONF_GRID_NOTE,
    CONF_GRID_FIXED_FEE,
    CONF_GRID_VARIABLE_FEE,
    CONF_GRID_FIXED_CREDIT,
    CONF_GRID_VARIABLE_CREDIT,
    CONF_GRID_ENERGY_TAX,
    CONF_ELECTRICITY_VAT,
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_FORECAST_ENTITY,
    CONF_POWER_ENTITY,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_POOL_PUMP_POWER_ENTITY,
    CONF_POOL_PUMP_POWER_W,
    CONF_WATER_HEATER_MAX_HOURS,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
    DOMAIN,
)
from .config_flow_helpers import (
    ALL_OPTIMIZER_ENTITY_KEYS,
    ALL_OPTIMIZER_NUMERIC_KEYS,
    BATTERY_STEP_ENTITY_KEYS,
    BATTERY_STEP_NUMERIC_KEYS,
    FLEXIBLE_LOADS_ENTITY_KEYS,
    FLEXIBLE_LOADS_NUMERIC_KEYS,
    GRID_METERING_ENTITY_KEYS,
    HOUSEHOLD_BINARY_ENTITY_KEYS,
    HOUSEHOLD_SENSOR_ENTITY_KEYS,
    HOT_WATER_ENTITY_KEYS,
    HOT_WATER_NUMERIC_KEYS,
    _build_battery_schema,
    _build_flexible_loads_schema,
    _build_grid_metering_schema,
    _build_household_schema,
    _build_hot_water_schema,
    _build_solar_forecast_schema,
    DEV_DEFAULTS,
    DEV_DEFAULTS_ENABLED,
    LEGACY_DEV_DEFAULT_ALIASES,
    _parse_unit_of_measurement,
    _schema_default,
    _validate_battery_settings,
    _validate_nordpool_prices_sensor,
    _validate_optional_sensor_entities,
    _validate_solar_forecast_entities,
)


def _dev_default(*keys: str):
    """Return the first matching dev default when DEV_DEFAULTS_ENABLED."""
    if not DEV_DEFAULTS_ENABLED:
        return None
    for key in keys:
        if key in DEV_DEFAULTS:
            return DEV_DEFAULTS[key]
    return None


def _form_value(data: dict[str, Any], key: str) -> Any:
    """Return stored value or matching dev default for a config key."""
    value = data.get(key)
    if value is not None:
        return value
    return _dev_default(key, *LEGACY_DEV_DEFAULT_ALIASES.get(key, ()))


class EnergyAdvisorFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self.data = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EnergyAdvisorOptionFlowHandler:
        return EnergyAdvisorOptionFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]
            is_valid, attributes = await _validate_nordpool_prices_sensor(
                self.hass, prices_sensor
            )

            if is_valid and attributes is not None:
                await self.async_set_unique_id(prices_sensor)
                self._abort_if_unique_id_configured()
                self.data.update(user_input)
                self.data.update(attributes)
                return await self.async_step_supplier_fees_and_credits()
            else:
                errors[CONF_NORDPOOL_PRICES_SENSOR] = "invalid_sensor"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NORDPOOL_PRICES_SENSOR,
                        default=(
                            user_input.get(CONF_NORDPOOL_PRICES_SENSOR)
                            if user_input
                            else (
                                _dev_default(CONF_NORDPOOL_PRICES_SENSOR)
                                or vol.UNDEFINED
                            )
                        ),
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain=SENSOR_DOMAIN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_supplier_fees_and_credits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            self.data[CONF_SUPPLIER_NOTE] = user_input.get(CONF_SUPPLIER_NOTE)
            self.data[CONF_SUPPLIER_FIXED_FEE] = user_input.get(CONF_SUPPLIER_FIXED_FEE)
            self.data[CONF_SUPPLIER_VARIABLE_FEE] = user_input.get(
                CONF_SUPPLIER_VARIABLE_FEE
            )
            self.data[CONF_SUPPLIER_FIXED_CREDIT] = user_input.get(
                CONF_SUPPLIER_FIXED_CREDIT
            )
            self.data[CONF_SUPPLIER_VARIABLE_CREDIT] = user_input.get(
                CONF_SUPPLIER_VARIABLE_CREDIT
            )
            return await self.async_step_grid_fees_and_credits()

        # Pre-fill form with existing data if any (e.g., when returning from a later step or error)
        supplier_note = self.data.get(CONF_SUPPLIER_NOTE)
        supplier_fixed_fee = (
            self.data.get(CONF_SUPPLIER_FIXED_FEE)
            if self.data.get(CONF_SUPPLIER_FIXED_FEE) is not None
            else _dev_default(CONF_SUPPLIER_FIXED_FEE)
        )
        supplier_variable_fee = (
            self.data.get(CONF_SUPPLIER_VARIABLE_FEE)
            if self.data.get(CONF_SUPPLIER_VARIABLE_FEE) is not None
            else _dev_default(CONF_SUPPLIER_VARIABLE_FEE)
        )
        supplier_fixed_credit = (
            self.data.get(CONF_SUPPLIER_FIXED_CREDIT)
            if self.data.get(CONF_SUPPLIER_FIXED_CREDIT) is not None
            else _dev_default(CONF_SUPPLIER_FIXED_CREDIT)
        )
        supplier_variable_credit = (
            self.data.get(CONF_SUPPLIER_VARIABLE_CREDIT)
            if self.data.get(CONF_SUPPLIER_VARIABLE_CREDIT) is not None
            else _dev_default(CONF_SUPPLIER_VARIABLE_CREDIT)
        )

        return self.async_show_form(
            step_id="supplier_fees_and_credits",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SUPPLIER_NOTE,
                        default=(
                            supplier_note
                            if supplier_note is not None
                            else vol.UNDEFINED
                        ),
                    ): vol.Coerce(str),
                    vol.Optional(
                        CONF_SUPPLIER_FIXED_FEE,
                        default=(
                            supplier_fixed_fee
                            if supplier_fixed_fee is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_VARIABLE_FEE,
                        default=(
                            supplier_variable_fee
                            if supplier_variable_fee is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_FIXED_CREDIT,
                        default=(
                            supplier_fixed_credit
                            if supplier_fixed_credit is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_VARIABLE_CREDIT,
                        default=(
                            supplier_variable_credit
                            if supplier_variable_credit is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_grid_fees_and_credits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            self.data[CONF_GRID_NOTE] = user_input.get(CONF_GRID_NOTE)
            self.data[CONF_GRID_FIXED_FEE] = user_input.get(CONF_GRID_FIXED_FEE)
            self.data[CONF_GRID_VARIABLE_FEE] = user_input.get(CONF_GRID_VARIABLE_FEE)
            self.data[CONF_GRID_FIXED_CREDIT] = user_input.get(CONF_GRID_FIXED_CREDIT)
            self.data[CONF_GRID_VARIABLE_CREDIT] = user_input.get(
                CONF_GRID_VARIABLE_CREDIT
            )
            return await self.async_step_taxes_and_vat()

        grid_note = self.data.get(CONF_GRID_NOTE)
        grid_fixed_fee = (
            self.data.get(CONF_GRID_FIXED_FEE)
            if self.data.get(CONF_GRID_FIXED_FEE) is not None
            else _dev_default(CONF_GRID_FIXED_FEE)
        )
        grid_variable_fee = (
            self.data.get(CONF_GRID_VARIABLE_FEE)
            if self.data.get(CONF_GRID_VARIABLE_FEE) is not None
            else _dev_default(CONF_GRID_VARIABLE_FEE)
        )
        grid_fixed_credit = (
            self.data.get(CONF_GRID_FIXED_CREDIT)
            if self.data.get(CONF_GRID_FIXED_CREDIT) is not None
            else _dev_default(CONF_GRID_FIXED_CREDIT)
        )
        grid_variable_credit = (
            self.data.get(CONF_GRID_VARIABLE_CREDIT)
            if self.data.get(CONF_GRID_VARIABLE_CREDIT) is not None
            else _dev_default(CONF_GRID_VARIABLE_CREDIT)
        )

        return self.async_show_form(
            step_id="grid_fees_and_credits",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GRID_NOTE,
                        default=grid_note if grid_note is not None else vol.UNDEFINED,
                    ): vol.Coerce(str),
                    vol.Optional(
                        CONF_GRID_FIXED_FEE,
                        default=(
                            grid_fixed_fee
                            if grid_fixed_fee is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_VARIABLE_FEE,
                        default=(
                            grid_variable_fee
                            if grid_variable_fee is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_FIXED_CREDIT,
                        default=(
                            grid_fixed_credit
                            if grid_fixed_credit is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_VARIABLE_CREDIT,
                        default=(
                            grid_variable_credit
                            if grid_variable_credit is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_taxes_and_vat(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            self.data[CONF_GRID_ENERGY_TAX] = user_input.get(CONF_GRID_ENERGY_TAX)
            self.data[CONF_ELECTRICITY_VAT] = user_input.get(CONF_ELECTRICITY_VAT)
            return await self.async_step_thresholds()

        grid_energy_tax = (
            self.data.get(CONF_GRID_ENERGY_TAX)
            if self.data.get(CONF_GRID_ENERGY_TAX) is not None
            else _dev_default(CONF_GRID_ENERGY_TAX)
        )
        electricity_vat = (
            self.data.get(CONF_ELECTRICITY_VAT)
            if self.data.get(CONF_ELECTRICITY_VAT) is not None
            else _dev_default(CONF_ELECTRICITY_VAT)
        )

        return self.async_show_form(
            step_id="taxes_and_vat",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GRID_ENERGY_TAX,
                        default=(
                            grid_energy_tax
                            if grid_energy_tax is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_ELECTRICITY_VAT,
                        default=(
                            electricity_vat
                            if electricity_vat is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            low_threshold = user_input.get(CONF_LOW_THRESHOLD)
            high_threshold = user_input.get(CONF_HIGH_THRESHOLD)

            if (
                low_threshold is not None
                and high_threshold is not None
                and low_threshold >= high_threshold
            ):
                errors["base"] = "low_threshold_higher_than_high_threshold"
            else:
                self.data[CONF_LOW_THRESHOLD] = low_threshold
                self.data[CONF_HIGH_THRESHOLD] = high_threshold
                return await self.async_step_solar_forecast()

        low_threshold_val = (
            self.data.get(CONF_LOW_THRESHOLD)
            if self.data.get(CONF_LOW_THRESHOLD) is not None
            else _dev_default(CONF_LOW_THRESHOLD)
        )
        high_threshold_val = (
            self.data.get(CONF_HIGH_THRESHOLD)
            if self.data.get(CONF_HIGH_THRESHOLD) is not None
            else _dev_default(CONF_HIGH_THRESHOLD)
        )

        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOW_THRESHOLD,
                        default=(
                            low_threshold_val
                            if low_threshold_val is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_HIGH_THRESHOLD,
                        default=(
                            high_threshold_val
                            if high_threshold_val is not None
                            else vol.UNDEFINED
                        ),
                        description={"suffix": unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_solar_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            forecast_entity = user_input.get(CONF_FORECAST_ENTITY)
            power_entity = user_input.get(CONF_POWER_ENTITY)
            tomorrow_entity = user_input.get(CONF_FORECAST_TOMORROW_ENTITY)

            errors.update(
                _validate_solar_forecast_entities(
                    self.hass,
                    forecast_entity,
                    power_entity,
                    tomorrow_entity,
                )
            )

            if not errors:
                self.data[CONF_FORECAST_ENTITY] = forecast_entity or None
                self.data[CONF_POWER_ENTITY] = power_entity or None
                self.data[CONF_FORECAST_TOMORROW_ENTITY] = tomorrow_entity or None
                return await self.async_step_battery()

        forecast_entity_val = self.data.get(CONF_FORECAST_ENTITY) or _dev_default(
            CONF_FORECAST_ENTITY
        )
        power_entity_val = self.data.get(CONF_POWER_ENTITY) or _dev_default(
            CONF_POWER_ENTITY
        )
        tomorrow_entity_val = self.data.get(
            CONF_FORECAST_TOMORROW_ENTITY
        ) or _dev_default(CONF_FORECAST_TOMORROW_ENTITY)

        return self.async_show_form(
            step_id="solar_forecast",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_FORECAST_ENTITY,
                        default=_schema_default(forecast_entity_val),
                        description={"suggested_value": forecast_entity_val},
                    ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
                    vol.Optional(
                        CONF_POWER_ENTITY,
                        default=_schema_default(power_entity_val),
                        description={"suggested_value": power_entity_val},
                    ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
                    vol.Optional(
                        CONF_FORECAST_TOMORROW_ENTITY,
                        default=_schema_default(tomorrow_entity_val),
                        description={"suggested_value": tomorrow_entity_val},
                    ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
                }
            ),
            errors=errors,
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            battery_entities = {
                key: user_input.get(key) for key in BATTERY_STEP_ENTITY_KEYS
            }
            errors.update(
                _validate_battery_settings(
                    user_input.get(CONF_BATTERY_CAPACITY_KWH),
                    user_input.get(CONF_BATTERY_MAX_CHARGE_POWER_W),
                )
            )
            if not errors:
                errors.update(
                    _validate_optional_sensor_entities(self.hass, battery_entities)
                )
            if not errors:
                for key in BATTERY_STEP_NUMERIC_KEYS:
                    self.data[key] = user_input.get(key)
                for key, entity_id in battery_entities.items():
                    self.data[key] = entity_id or None
                return await self.async_step_grid_metering()

        form_values = {
            key: _form_value(self.data, key)
            for key in (*BATTERY_STEP_NUMERIC_KEYS, *BATTERY_STEP_ENTITY_KEYS)
        }
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema(
                _build_battery_schema(form_values, unit_of_measurement)
            ),
            errors=errors,
        )

    async def async_step_grid_metering(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            grid_entities = {
                key: user_input.get(key) for key in GRID_METERING_ENTITY_KEYS
            }
            errors.update(_validate_optional_sensor_entities(self.hass, grid_entities))
            if not errors:
                for key, entity_id in grid_entities.items():
                    self.data[key] = entity_id or None
                return await self.async_step_household()

        form_values = {
            key: _form_value(self.data, key) for key in GRID_METERING_ENTITY_KEYS
        }
        return self.async_show_form(
            step_id="grid_metering",
            data_schema=vol.Schema(_build_grid_metering_schema(form_values)),
            errors=errors,
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            household_entities = {
                key: user_input.get(key)
                for key in (
                    *HOUSEHOLD_SENSOR_ENTITY_KEYS,
                    *HOUSEHOLD_BINARY_ENTITY_KEYS,
                )
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, household_entities)
            )
            if not errors:
                for key, entity_id in household_entities.items():
                    self.data[key] = entity_id or None
                return await self.async_step_hot_water()

        form_values = {
            key: _form_value(self.data, key)
            for key in (*HOUSEHOLD_SENSOR_ENTITY_KEYS, *HOUSEHOLD_BINARY_ENTITY_KEYS)
        }
        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(_build_household_schema(form_values)),
            errors=errors,
        )

    async def async_step_hot_water(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            hot_water_entities = {
                key: user_input.get(key) for key in HOT_WATER_ENTITY_KEYS
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, hot_water_entities)
            )
            if not errors:
                for key, entity_id in hot_water_entities.items():
                    self.data[key] = entity_id or None
                for key in HOT_WATER_NUMERIC_KEYS:
                    self.data[key] = user_input.get(key)
                return await self.async_step_flexible_loads()

        form_values = {
            key: _form_value(self.data, key)
            for key in (*HOT_WATER_ENTITY_KEYS, *HOT_WATER_NUMERIC_KEYS)
        }
        return self.async_show_form(
            step_id="hot_water",
            data_schema=vol.Schema(_build_hot_water_schema(form_values)),
            errors=errors,
        )

    async def async_step_flexible_loads(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            flexible_entities = {
                key: user_input.get(key) for key in FLEXIBLE_LOADS_ENTITY_KEYS
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, flexible_entities)
            )
            if not errors:
                for key, entity_id in flexible_entities.items():
                    self.data[key] = entity_id or None
                for key in FLEXIBLE_LOADS_NUMERIC_KEYS:
                    self.data[key] = user_input.get(key)
                return self.async_create_entry(
                    title="Energy Advisor",
                    data=self.data,
                    options={
                        CONF_NORDPOOL_PRICES_SENSOR: self.data[
                            CONF_NORDPOOL_PRICES_SENSOR
                        ],
                        "unit_of_measurement": self.data.get("unit_of_measurement", ""),
                        "currency": self.data.get("currency", ""),
                        "energy_unit": self.data.get("energy_unit", ""),
                        "price_divisor": self.data.get("price_divisor", 100),
                        CONF_SUPPLIER_NOTE: self.data.get(CONF_SUPPLIER_NOTE),
                        CONF_SUPPLIER_FIXED_FEE: self.data.get(CONF_SUPPLIER_FIXED_FEE),
                        CONF_SUPPLIER_VARIABLE_FEE: self.data.get(
                            CONF_SUPPLIER_VARIABLE_FEE
                        ),
                        CONF_SUPPLIER_FIXED_CREDIT: self.data.get(
                            CONF_SUPPLIER_FIXED_CREDIT
                        ),
                        CONF_SUPPLIER_VARIABLE_CREDIT: self.data.get(
                            CONF_SUPPLIER_VARIABLE_CREDIT
                        ),
                        CONF_GRID_NOTE: self.data.get(CONF_GRID_NOTE),
                        CONF_GRID_FIXED_FEE: self.data.get(CONF_GRID_FIXED_FEE),
                        CONF_GRID_VARIABLE_FEE: self.data.get(CONF_GRID_VARIABLE_FEE),
                        CONF_GRID_FIXED_CREDIT: self.data.get(CONF_GRID_FIXED_CREDIT),
                        CONF_GRID_VARIABLE_CREDIT: self.data.get(
                            CONF_GRID_VARIABLE_CREDIT
                        ),
                        CONF_ELECTRICITY_VAT: self.data.get(CONF_ELECTRICITY_VAT),
                        CONF_GRID_ENERGY_TAX: self.data.get(CONF_GRID_ENERGY_TAX),
                        CONF_LOW_THRESHOLD: self.data.get(CONF_LOW_THRESHOLD),
                        CONF_HIGH_THRESHOLD: self.data.get(CONF_HIGH_THRESHOLD),
                        CONF_EXCLUDE_FROM_RECORDING: True,
                        CONF_FORECAST_ENTITY: self.data.get(CONF_FORECAST_ENTITY),
                        CONF_POWER_ENTITY: self.data.get(CONF_POWER_ENTITY),
                        CONF_FORECAST_TOMORROW_ENTITY: self.data.get(
                            CONF_FORECAST_TOMORROW_ENTITY
                        ),
                        CONF_BATTERY_CAPACITY_KWH: self.data.get(
                            CONF_BATTERY_CAPACITY_KWH
                        ),
                        CONF_BATTERY_MAX_CHARGE_POWER_W: self.data.get(
                            CONF_BATTERY_MAX_CHARGE_POWER_W
                        ),
                        CONF_BATTERY_DEGRADATION_COST: self.data.get(
                            CONF_BATTERY_DEGRADATION_COST
                        ),
                        **{
                            key: self.data.get(key) for key in ALL_OPTIMIZER_ENTITY_KEYS
                        },
                        **{
                            key: self.data.get(key)
                            for key in ALL_OPTIMIZER_NUMERIC_KEYS
                        },
                    },
                )

        form_values = {
            key: _form_value(self.data, key)
            for key in (*FLEXIBLE_LOADS_ENTITY_KEYS, *FLEXIBLE_LOADS_NUMERIC_KEYS)
        }
        return self.async_show_form(
            step_id="flexible_loads",
            data_schema=vol.Schema(_build_flexible_loads_schema(form_values)),
            errors=errors,
        )


class EnergyAdvisorOptionFlowHandler(OptionsFlow):
    def __init__(self) -> None:
        self.current_options: dict[str, Any] = {}
        self.unit_of_measurement = ""

    def _update_price_sensor_attributes(
        self, sensor_attributes: dict[str, Any] | None
    ) -> None:
        """Persist display metadata for the selected price sensor."""
        if sensor_attributes:
            self.current_options["unit_of_measurement"] = sensor_attributes.get(
                "unit_of_measurement", ""
            )
            self.current_options["currency"] = sensor_attributes.get("currency", "")
            self.current_options["energy_unit"] = sensor_attributes.get(
                "energy_unit", ""
            )
            self.current_options["price_divisor"] = sensor_attributes.get(
                "price_divisor", 100
            )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if not self.current_options:
            self.current_options = dict(self.config_entry.options)

        current_prices_sensor = self.current_options.get(
            CONF_NORDPOOL_PRICES_SENSOR, ""
        )
        if user_input and CONF_NORDPOOL_PRICES_SENSOR in user_input:
            current_prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]

        _is_valid_for_display, display_attributes = (
            await _validate_nordpool_prices_sensor(self.hass, current_prices_sensor)
        )
        self.unit_of_measurement = (
            display_attributes.get("unit_of_measurement", "")
            if display_attributes
            else ""
        )

        if user_input is not None:
            submitted_prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]
            is_valid_sensor, sensor_attributes = await _validate_nordpool_prices_sensor(
                self.hass, submitted_prices_sensor
            )
            if not is_valid_sensor:
                errors[CONF_NORDPOOL_PRICES_SENSOR] = "invalid_sensor"
            else:
                self.unit_of_measurement = (
                    sensor_attributes.get("unit_of_measurement", "")
                    if sensor_attributes
                    else ""
                )
                low_threshold = user_input.get(CONF_LOW_THRESHOLD)
                high_threshold = user_input.get(CONF_HIGH_THRESHOLD)
                if (
                    low_threshold is not None
                    and high_threshold is not None
                    and low_threshold >= high_threshold
                ):
                    errors["base"] = "low_threshold_higher_than_high_threshold"

                if not errors:
                    self.current_options.update(user_input)
                    self._update_price_sensor_attributes(sensor_attributes)
                    return await self.async_step_supplier_fees_and_credits()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NORDPOOL_PRICES_SENSOR,
                        default=self.current_options.get(
                            CONF_NORDPOOL_PRICES_SENSOR, ""
                        ),
                    ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
                    vol.Optional(
                        CONF_LOW_THRESHOLD,
                        description={
                            "suggested_value": self.current_options.get(
                                CONF_LOW_THRESHOLD
                            ),
                            "suffix": self.unit_of_measurement,
                        },
                        default=self.current_options.get(CONF_LOW_THRESHOLD),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_HIGH_THRESHOLD,
                        description={
                            "suggested_value": self.current_options.get(
                                CONF_HIGH_THRESHOLD
                            ),
                            "suffix": self.unit_of_measurement,
                        },
                        default=self.current_options.get(CONF_HIGH_THRESHOLD),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_supplier_fees_and_credits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self.current_options.update(user_input)
            return await self.async_step_grid_fees_and_credits()

        return self.async_show_form(
            step_id="supplier_fees_and_credits",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SUPPLIER_NOTE,
                        default=_schema_default(
                            self.current_options.get(CONF_SUPPLIER_NOTE)
                        ),
                    ): vol.Coerce(str),
                    vol.Optional(
                        CONF_SUPPLIER_FIXED_FEE,
                        default=_schema_default(
                            self.current_options.get(CONF_SUPPLIER_FIXED_FEE)
                        ),
                        description={"suffix": self.unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_VARIABLE_FEE,
                        default=_schema_default(
                            self.current_options.get(CONF_SUPPLIER_VARIABLE_FEE)
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_FIXED_CREDIT,
                        default=_schema_default(
                            self.current_options.get(CONF_SUPPLIER_FIXED_CREDIT)
                        ),
                        description={"suffix": self.unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_SUPPLIER_VARIABLE_CREDIT,
                        default=_schema_default(
                            self.current_options.get(CONF_SUPPLIER_VARIABLE_CREDIT)
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_grid_fees_and_credits(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self.current_options.update(user_input)
            return await self.async_step_taxes_and_vat()

        return self.async_show_form(
            step_id="grid_fees_and_credits",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GRID_NOTE,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_NOTE)
                        ),
                    ): vol.Coerce(str),
                    vol.Optional(
                        CONF_GRID_FIXED_FEE,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_FIXED_FEE)
                        ),
                        description={"suffix": self.unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_VARIABLE_FEE,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_VARIABLE_FEE)
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_FIXED_CREDIT,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_FIXED_CREDIT)
                        ),
                        description={"suffix": self.unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_GRID_VARIABLE_CREDIT,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_VARIABLE_CREDIT)
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_taxes_and_vat(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self.current_options.update(user_input)
            return await self.async_step_solar_forecast()

        return self.async_show_form(
            step_id="taxes_and_vat",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GRID_ENERGY_TAX,
                        default=_schema_default(
                            self.current_options.get(CONF_GRID_ENERGY_TAX)
                        ),
                        description={"suffix": self.unit_of_measurement},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_ELECTRICITY_VAT,
                        default=_schema_default(
                            self.current_options.get(CONF_ELECTRICITY_VAT)
                        ),
                        description={"suffix": "%"},
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_solar_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            errors.update(
                _validate_solar_forecast_entities(
                    self.hass,
                    user_input.get(CONF_FORECAST_ENTITY),
                    user_input.get(CONF_POWER_ENTITY),
                    user_input.get(CONF_FORECAST_TOMORROW_ENTITY),
                )
            )
            if not errors:
                self.current_options.update(user_input)
                return await self.async_step_battery()

        form_values = {
            key: _form_value(self.current_options, key)
            for key in (
                CONF_FORECAST_ENTITY,
                CONF_POWER_ENTITY,
                CONF_FORECAST_TOMORROW_ENTITY,
            )
        }
        return self.async_show_form(
            step_id="solar_forecast",
            data_schema=vol.Schema(_build_solar_forecast_schema(form_values)),
            errors=errors,
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            battery_entities = {
                key: user_input.get(key) for key in BATTERY_STEP_ENTITY_KEYS
            }
            errors.update(
                _validate_battery_settings(
                    user_input.get(CONF_BATTERY_CAPACITY_KWH),
                    user_input.get(CONF_BATTERY_MAX_CHARGE_POWER_W),
                )
            )
            if not errors:
                errors.update(
                    _validate_optional_sensor_entities(self.hass, battery_entities)
                )
            if not errors:
                self.current_options.update(user_input)
                return await self.async_step_grid_metering()

        form_values = {
            key: _form_value(self.current_options, key)
            for key in (*BATTERY_STEP_NUMERIC_KEYS, *BATTERY_STEP_ENTITY_KEYS)
        }
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema(
                _build_battery_schema(form_values, self.unit_of_measurement)
            ),
            errors=errors,
        )

    async def async_step_grid_metering(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            grid_entities = {
                key: user_input.get(key) for key in GRID_METERING_ENTITY_KEYS
            }
            errors.update(_validate_optional_sensor_entities(self.hass, grid_entities))
            if not errors:
                self.current_options.update(user_input)
                return await self.async_step_household()

        form_values = {
            key: _form_value(self.current_options, key)
            for key in GRID_METERING_ENTITY_KEYS
        }
        return self.async_show_form(
            step_id="grid_metering",
            data_schema=vol.Schema(_build_grid_metering_schema(form_values)),
            errors=errors,
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            household_entities = {
                key: user_input.get(key)
                for key in (
                    *HOUSEHOLD_SENSOR_ENTITY_KEYS,
                    *HOUSEHOLD_BINARY_ENTITY_KEYS,
                )
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, household_entities)
            )
            if not errors:
                self.current_options.update(user_input)
                return await self.async_step_hot_water()

        form_values = {
            key: _form_value(self.current_options, key)
            for key in (*HOUSEHOLD_SENSOR_ENTITY_KEYS, *HOUSEHOLD_BINARY_ENTITY_KEYS)
        }
        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(_build_household_schema(form_values)),
            errors=errors,
        )

    async def async_step_hot_water(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            hot_water_entities = {
                key: user_input.get(key) for key in HOT_WATER_ENTITY_KEYS
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, hot_water_entities)
            )
            if not errors:
                self.current_options.update(user_input)
                return await self.async_step_flexible_loads()

        form_values = {
            key: _form_value(self.current_options, key)
            for key in (*HOT_WATER_ENTITY_KEYS, *HOT_WATER_NUMERIC_KEYS)
        }
        return self.async_show_form(
            step_id="hot_water",
            data_schema=vol.Schema(_build_hot_water_schema(form_values)),
            errors=errors,
        )

    async def async_step_flexible_loads(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            flexible_entities = {
                key: user_input.get(key) for key in FLEXIBLE_LOADS_ENTITY_KEYS
            }
            errors.update(
                _validate_optional_sensor_entities(self.hass, flexible_entities)
            )
            if not errors:
                self.current_options.update(user_input)
                for key in (
                    CONF_FORECAST_ENTITY,
                    CONF_POWER_ENTITY,
                    CONF_FORECAST_TOMORROW_ENTITY,
                    *ALL_OPTIMIZER_ENTITY_KEYS,
                ):
                    self.current_options[key] = self.current_options.get(key) or None
                for key in (
                    *BATTERY_STEP_NUMERIC_KEYS,
                    *ALL_OPTIMIZER_NUMERIC_KEYS,
                ):
                    self.current_options[key] = (
                        self.current_options[key]
                        if key in self.current_options
                        else None
                    )
                return self.async_create_entry(title="", data=self.current_options)

        form_values = {
            key: _form_value(self.current_options, key)
            for key in (*FLEXIBLE_LOADS_ENTITY_KEYS, *FLEXIBLE_LOADS_NUMERIC_KEYS)
        }
        return self.async_show_form(
            step_id="flexible_loads",
            data_schema=vol.Schema(_build_flexible_loads_schema(form_values)),
            errors=errors,
        )


# The validate_float function is not used in the provided code, can be removed if not needed elsewhere.
# def validate_float(value):
#     try:
#         float_value = float(value)
#         _LOGGER.debug(f"Validated float value: {float_value}")
#         return float_value
#     except ValueError:
#         _LOGGER.error("Invalid float value")
#         raise vol.Invalid("Invalid float value")
