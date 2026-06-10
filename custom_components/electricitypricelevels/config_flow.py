"""Config flow for ElectricityPriceLevel integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
import homeassistant.helpers.config_validation as cv

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback, HomeAssistant
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN


from .const import (
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_LOW_THRESHOLD,
    CONF_HIGH_THRESHOLD,
    CONF_SUPPLIER_NOTE,
    CONF_SUPPLIER_FIXED_FEE,
    CONF_SUPPLIER_VARIABLE_FEE,
    CONF_SUPPLIER_FIXED_CREDIT,
    CONF_SUPPLIER_VARIABLE_CREDIT,
    CONF_GRID_NOTE,
    CONF_GRID_FIXED_FEE,
    CONF_GRID_VARIABLE_FEE,
    CONF_GRID_FIXED_CREDIT,
    CONF_GRID_VARIABLE_CREDIT,
    CONF_GRID_ENERGY_TAX,
    CONF_ELECTRICITY_VAT,
    CONF_EXCLUDE_FROM_RECORDING,
    DEV_DEFAULTS,
    DEV_DEFAULTS_ENABLED,
    DOMAIN,
    parse_unit_of_measurement,
)

_LOGGER = logging.getLogger(__name__)


def _parse_unit_of_measurement(unit_str: str) -> tuple[str | None, str | None]:
    """Delegate to shared implementation in const.py."""
    return parse_unit_of_measurement(unit_str)


def _dev_default(key: str):
    """Return the dev default for key when DEV_DEFAULTS_ENABLED, else None."""
    return DEV_DEFAULTS.get(key) if DEV_DEFAULTS_ENABLED else None


async def _validate_nordpool_prices_sensor(hass: HomeAssistant, entity_id: str) -> tuple[bool, dict | None]:
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


class ElectricityPriceLevelFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self.data = {}

    @staticmethod
    @callback
    def async_get_options_flow(
            config_entry: ConfigEntry,
    ) -> ElectricityPriceLevelOptionFlowHandler:
        return ElectricityPriceLevelOptionFlowHandler()

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]
            is_valid, attributes = await _validate_nordpool_prices_sensor(self.hass, prices_sensor)

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
            data_schema=vol.Schema({
                vol.Required(CONF_NORDPOOL_PRICES_SENSOR, default=(
                    user_input.get(CONF_NORDPOOL_PRICES_SENSOR)
                    if user_input
                    else (_dev_default(CONF_NORDPOOL_PRICES_SENSOR) or vol.UNDEFINED)
                )): EntitySelector(
                    EntitySelectorConfig(
                        domain=SENSOR_DOMAIN,
                    )
                ),
            }),
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
            self.data[CONF_SUPPLIER_VARIABLE_FEE] = user_input.get(CONF_SUPPLIER_VARIABLE_FEE)
            self.data[CONF_SUPPLIER_FIXED_CREDIT] = user_input.get(CONF_SUPPLIER_FIXED_CREDIT)
            self.data[CONF_SUPPLIER_VARIABLE_CREDIT] = user_input.get(CONF_SUPPLIER_VARIABLE_CREDIT)
            return await self.async_step_grid_fees_and_credits()

        # Pre-fill form with existing data if any (e.g., when returning from a later step or error)
        supplier_note = self.data.get(CONF_SUPPLIER_NOTE)
        supplier_fixed_fee = self.data.get(CONF_SUPPLIER_FIXED_FEE) if self.data.get(CONF_SUPPLIER_FIXED_FEE) is not None else _dev_default(CONF_SUPPLIER_FIXED_FEE)
        supplier_variable_fee = self.data.get(CONF_SUPPLIER_VARIABLE_FEE) if self.data.get(CONF_SUPPLIER_VARIABLE_FEE) is not None else _dev_default(CONF_SUPPLIER_VARIABLE_FEE)
        supplier_fixed_credit = self.data.get(CONF_SUPPLIER_FIXED_CREDIT) if self.data.get(CONF_SUPPLIER_FIXED_CREDIT) is not None else _dev_default(CONF_SUPPLIER_FIXED_CREDIT)
        supplier_variable_credit = self.data.get(CONF_SUPPLIER_VARIABLE_CREDIT) if self.data.get(CONF_SUPPLIER_VARIABLE_CREDIT) is not None else _dev_default(CONF_SUPPLIER_VARIABLE_CREDIT)

        return self.async_show_form(
            step_id="supplier_fees_and_credits",
            data_schema=vol.Schema({
                vol.Optional(CONF_SUPPLIER_NOTE, default=supplier_note if supplier_note is not None else vol.UNDEFINED): vol.Coerce(str),
                vol.Optional(CONF_SUPPLIER_FIXED_FEE, default=supplier_fixed_fee if supplier_fixed_fee is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(CONF_SUPPLIER_VARIABLE_FEE, default=supplier_variable_fee if supplier_variable_fee is not None else vol.UNDEFINED, description={"suffix": "%"}): vol.All(vol.Coerce(float),vol.Range(min=0)),
                vol.Optional(CONF_SUPPLIER_FIXED_CREDIT, default=supplier_fixed_credit if supplier_fixed_credit is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float),vol.Range(min=0)),
                vol.Optional(CONF_SUPPLIER_VARIABLE_CREDIT, default=supplier_variable_credit if supplier_variable_credit is not None else vol.UNDEFINED, description={"suffix": "%"}): vol.All(vol.Coerce(float),vol.Range(min=0)),
            }),
            errors=errors
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
            self.data[CONF_GRID_VARIABLE_CREDIT] = user_input.get(CONF_GRID_VARIABLE_CREDIT)
            return await self.async_step_taxes_and_vat()

        grid_note = self.data.get(CONF_GRID_NOTE)
        grid_fixed_fee = self.data.get(CONF_GRID_FIXED_FEE) if self.data.get(CONF_GRID_FIXED_FEE) is not None else _dev_default(CONF_GRID_FIXED_FEE)
        grid_variable_fee = self.data.get(CONF_GRID_VARIABLE_FEE) if self.data.get(CONF_GRID_VARIABLE_FEE) is not None else _dev_default(CONF_GRID_VARIABLE_FEE)
        grid_fixed_credit = self.data.get(CONF_GRID_FIXED_CREDIT) if self.data.get(CONF_GRID_FIXED_CREDIT) is not None else _dev_default(CONF_GRID_FIXED_CREDIT)
        grid_variable_credit = self.data.get(CONF_GRID_VARIABLE_CREDIT) if self.data.get(CONF_GRID_VARIABLE_CREDIT) is not None else _dev_default(CONF_GRID_VARIABLE_CREDIT)

        return self.async_show_form(
            step_id="grid_fees_and_credits",
            data_schema=vol.Schema({
                vol.Optional(CONF_GRID_NOTE, default=grid_note if grid_note is not None else vol.UNDEFINED): vol.Coerce(str),
                vol.Optional(CONF_GRID_FIXED_FEE, default=grid_fixed_fee if grid_fixed_fee is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(CONF_GRID_VARIABLE_FEE, default=grid_variable_fee if grid_variable_fee is not None else vol.UNDEFINED, description={"suffix": "%"}): vol.All(vol.Coerce(float),vol.Range(min=0)),
                vol.Optional(CONF_GRID_FIXED_CREDIT, default=grid_fixed_credit if grid_fixed_credit is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float),vol.Range(min=0)),
                vol.Optional(CONF_GRID_VARIABLE_CREDIT, default=grid_variable_credit if grid_variable_credit is not None else vol.UNDEFINED, description={"suffix": "%"}): vol.All(vol.Coerce(float),vol.Range(min=0)),
            }),
            errors=errors
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

        grid_energy_tax = self.data.get(CONF_GRID_ENERGY_TAX) if self.data.get(CONF_GRID_ENERGY_TAX) is not None else _dev_default(CONF_GRID_ENERGY_TAX)
        electricity_vat = self.data.get(CONF_ELECTRICITY_VAT) if self.data.get(CONF_ELECTRICITY_VAT) is not None else _dev_default(CONF_ELECTRICITY_VAT)

        return self.async_show_form(
            step_id="taxes_and_vat",
            data_schema=vol.Schema({
                vol.Optional(CONF_GRID_ENERGY_TAX, default=grid_energy_tax if grid_energy_tax is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(CONF_ELECTRICITY_VAT, default=electricity_vat if electricity_vat is not None else vol.UNDEFINED, description={"suffix": "%"}): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }),
            errors=errors
        )

    async def async_step_thresholds(
            self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        unit_of_measurement = self.data.get("unit_of_measurement", "")
        if user_input is not None:
            low_threshold = user_input.get(CONF_LOW_THRESHOLD)
            high_threshold = user_input.get(CONF_HIGH_THRESHOLD)

            if low_threshold is not None and high_threshold is not None and low_threshold >= high_threshold:
                errors["base"] = "low_threshold_higher_than_high_threshold"
            else:
                self.data[CONF_LOW_THRESHOLD] = low_threshold
                self.data[CONF_HIGH_THRESHOLD] = high_threshold
                # All data collected, create the entry
                # Options will be managed by the options flow
                return self.async_create_entry(
                    title="ElectricityPriceLevel", # Or a more dynamic title if desired
                    data=self.data,
                    options={ # Initialize options with data from main flow
                        CONF_NORDPOOL_PRICES_SENSOR: self.data[CONF_NORDPOOL_PRICES_SENSOR],
                        "unit_of_measurement": self.data.get("unit_of_measurement", ""),
                        "currency": self.data.get("currency", ""),
                        "energy_unit": self.data.get("energy_unit", ""),
                        "price_divisor": self.data.get("price_divisor", 100),
                        CONF_SUPPLIER_NOTE: self.data.get(CONF_SUPPLIER_NOTE),
                        CONF_SUPPLIER_FIXED_FEE: self.data.get(CONF_SUPPLIER_FIXED_FEE),
                        CONF_SUPPLIER_VARIABLE_FEE: self.data.get(CONF_SUPPLIER_VARIABLE_FEE),
                        CONF_SUPPLIER_FIXED_CREDIT: self.data.get(CONF_SUPPLIER_FIXED_CREDIT),
                        CONF_SUPPLIER_VARIABLE_CREDIT: self.data.get(CONF_SUPPLIER_VARIABLE_CREDIT),
                        CONF_GRID_NOTE: self.data.get(CONF_GRID_NOTE),
                        CONF_GRID_FIXED_FEE: self.data.get(CONF_GRID_FIXED_FEE),
                        CONF_GRID_VARIABLE_FEE: self.data.get(CONF_GRID_VARIABLE_FEE),
                        CONF_GRID_FIXED_CREDIT: self.data.get(CONF_GRID_FIXED_CREDIT),
                        CONF_GRID_VARIABLE_CREDIT: self.data.get(CONF_GRID_VARIABLE_CREDIT),
                        CONF_ELECTRICITY_VAT: self.data.get(CONF_ELECTRICITY_VAT),
                        CONF_GRID_ENERGY_TAX: self.data.get(CONF_GRID_ENERGY_TAX),
                        CONF_LOW_THRESHOLD: self.data.get(CONF_LOW_THRESHOLD),
                        CONF_HIGH_THRESHOLD: self.data.get(CONF_HIGH_THRESHOLD),
                        CONF_EXCLUDE_FROM_RECORDING: True,
                    }
                )

        low_threshold_val = self.data.get(CONF_LOW_THRESHOLD) if self.data.get(CONF_LOW_THRESHOLD) is not None else _dev_default(CONF_LOW_THRESHOLD)
        high_threshold_val = self.data.get(CONF_HIGH_THRESHOLD) if self.data.get(CONF_HIGH_THRESHOLD) is not None else _dev_default(CONF_HIGH_THRESHOLD)

        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema({
                vol.Optional(CONF_LOW_THRESHOLD, default=low_threshold_val if low_threshold_val is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(CONF_HIGH_THRESHOLD, default=high_threshold_val if high_threshold_val is not None else vol.UNDEFINED, description={"suffix": unit_of_measurement}): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }),
            errors=errors
        )


class ElectricityPriceLevelOptionFlowHandler(OptionsFlow):

    async def async_step_init(
            self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        self.current_options = dict(self.config_entry.options)
        # Determine unit_of_measurement based on currently saved (or about to be saved) prices sensor
        # This is primarily for display in the form. Validation of new sensor happens below.
        current_prices_sensor = self.current_options.get(CONF_NORDPOOL_PRICES_SENSOR, "")
        if user_input and CONF_NORDPOOL_PRICES_SENSOR in user_input: # If user is changing it
             current_prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]

        # Fetch attributes for the current prices sensor to display suffixes correctly
        # This doesn't validate it yet, just for display. Validation is on submit.
        _is_valid_for_display, display_attributes = await _validate_nordpool_prices_sensor(self.hass, current_prices_sensor)
        self.unit_of_measurement = display_attributes.get("unit_of_measurement", "") if display_attributes else ""


        if user_input is not None:
            # Validate the submitted Nordpool prices sensor
            submitted_prices_sensor = user_input[CONF_NORDPOOL_PRICES_SENSOR]
            is_valid_sensor, sensor_attributes = await _validate_nordpool_prices_sensor(self.hass, submitted_prices_sensor)

            if not is_valid_sensor:
                errors[CONF_NORDPOOL_PRICES_SENSOR] = "invalid_sensor"
            else:
                # Prices sensor is valid, update unit_of_measurement based on potentially new valid sensor
                self.unit_of_measurement = sensor_attributes.get("unit_of_measurement", "") if sensor_attributes else ""

                # Validate thresholds
                low_threshold = user_input.get(CONF_LOW_THRESHOLD)
                high_threshold = user_input.get(CONF_HIGH_THRESHOLD)

                if low_threshold is not None and high_threshold is not None and low_threshold >= high_threshold:
                    errors["base"] = "low_threshold_higher_than_high_threshold"

                if not errors:
                    # All validations passed, create/update the options entry
                    self.current_options.update(user_input)
                    # Ensure unit_of_measurement, currency, and energy_unit are updated if sensor changed
                    if sensor_attributes:
                        self.current_options["unit_of_measurement"] = sensor_attributes.get("unit_of_measurement", "")
                        self.current_options["currency"] = sensor_attributes.get("currency", "")
                        self.current_options["energy_unit"] = sensor_attributes.get("energy_unit", "")
                        self.current_options["price_divisor"] = sensor_attributes.get("price_divisor", 100)
                    return self.async_create_entry(title="", data=self.current_options)

        # Populate schema with current/suggested values
        schema_dict = {
            vol.Required(
                CONF_NORDPOOL_PRICES_SENSOR,
                default=self.current_options.get(CONF_NORDPOOL_PRICES_SENSOR, "")
            ): EntitySelector(
                EntitySelectorConfig(
                    domain=SENSOR_DOMAIN,
                )
            ),
            vol.Optional(
                CONF_LOW_THRESHOLD,
                description={"suggested_value": self.current_options.get(CONF_LOW_THRESHOLD), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_LOW_THRESHOLD)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_HIGH_THRESHOLD,
                description={"suggested_value": self.current_options.get(CONF_HIGH_THRESHOLD), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_HIGH_THRESHOLD)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_SUPPLIER_NOTE,
                description={"suggested_value": self.current_options.get(CONF_SUPPLIER_NOTE)},
                default=self.current_options.get(CONF_SUPPLIER_NOTE)
            ): vol.Coerce(str),
            vol.Optional(
                CONF_SUPPLIER_FIXED_FEE,
                description={"suggested_value": self.current_options.get(CONF_SUPPLIER_FIXED_FEE), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_SUPPLIER_FIXED_FEE)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_SUPPLIER_VARIABLE_FEE,
                description={"suggested_value": self.current_options.get(CONF_SUPPLIER_VARIABLE_FEE), "suffix": "%"},
                default=self.current_options.get(CONF_SUPPLIER_VARIABLE_FEE)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_SUPPLIER_FIXED_CREDIT,
                description={"suggested_value": self.current_options.get(CONF_SUPPLIER_FIXED_CREDIT), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_SUPPLIER_FIXED_CREDIT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_SUPPLIER_VARIABLE_CREDIT,
                description={"suggested_value": self.current_options.get(CONF_SUPPLIER_VARIABLE_CREDIT), "suffix": "%"},
                default=self.current_options.get(CONF_SUPPLIER_VARIABLE_CREDIT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_GRID_NOTE,
                description={"suggested_value": self.current_options.get(CONF_GRID_NOTE)},
                default=self.current_options.get(CONF_GRID_NOTE)
            ): vol.Coerce(str),
            vol.Optional(
                CONF_GRID_FIXED_FEE,
                description={"suggested_value": self.current_options.get(CONF_GRID_FIXED_FEE), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_GRID_FIXED_FEE)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_GRID_VARIABLE_FEE,
                description={"suggested_value": self.current_options.get(CONF_GRID_VARIABLE_FEE), "suffix": "%"},
                default=self.current_options.get(CONF_GRID_VARIABLE_FEE)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_GRID_FIXED_CREDIT,
                description={"suggested_value": self.current_options.get(CONF_GRID_FIXED_CREDIT), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_GRID_FIXED_CREDIT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_GRID_VARIABLE_CREDIT,
                description={"suggested_value": self.current_options.get(CONF_GRID_VARIABLE_CREDIT), "suffix": "%"},
                default=self.current_options.get(CONF_GRID_VARIABLE_CREDIT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_GRID_ENERGY_TAX,
                description={"suggested_value": self.current_options.get(CONF_GRID_ENERGY_TAX), "suffix": self.unit_of_measurement},
                default=self.current_options.get(CONF_GRID_ENERGY_TAX)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_ELECTRICITY_VAT,
                description={"suggested_value": self.current_options.get(CONF_ELECTRICITY_VAT), "suffix": "%"},
                default=self.current_options.get(CONF_ELECTRICITY_VAT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_EXCLUDE_FROM_RECORDING,
                default=self.current_options.get(CONF_EXCLUDE_FROM_RECORDING, True)
            ): cv.boolean,
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
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
