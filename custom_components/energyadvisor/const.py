"""Constants for the Energy Advisor integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry

DOMAIN = "energyadvisor"
LOGGER = logging.getLogger(__package__)

# Development configuration — imported from dev_config.py (gitignored).
# If the file is missing, dev features are disabled with safe defaults.
try:
    from . import dev_config as _dev_config  # noqa: F401
except ImportError:
    DEV_DEFAULTS_ENABLED = False
    DEV_DEFAULTS: dict = {}  # type: ignore[no-redef]
    HA_URL = ""
    HA_TOKEN = ""
else:
    DEV_DEFAULTS_ENABLED = getattr(_dev_config, "DEV_DEFAULTS_ENABLED", False)
    DEV_DEFAULTS = getattr(_dev_config, "DEV_DEFAULTS", {})
    HA_URL = getattr(_dev_config, "HA_URL", "")
    HA_TOKEN = getattr(_dev_config, "HA_TOKEN", "")

CONF_NORDPOOL_PRICES_SENSOR = "nordpool_prices_sensor"
CONF_LOW_THRESHOLD = "low_threshold"
CONF_HIGH_THRESHOLD = "high_threshold"
CONF_SUPPLIER_NOTE = "supplier_note"
CONF_SUPPLIER_FIXED_FEE = "supplier_fixed_fee"
CONF_SUPPLIER_VARIABLE_FEE = "supplier_variable_fee"
CONF_SUPPLIER_FIXED_CREDIT = "supplier_fixed_credit"
CONF_SUPPLIER_VARIABLE_CREDIT = "supplier_variable_credit"
CONF_GRID_NOTE = "grid_note"
CONF_GRID_FIXED_FEE = "grid_fixed_fee"
CONF_GRID_VARIABLE_FEE = "grid_variable_fee"
CONF_GRID_FIXED_CREDIT = "grid_fixed_credit"
CONF_GRID_VARIABLE_CREDIT = "grid_variable_credit"
CONF_GRID_ENERGY_TAX = "grid_energy_tax"
CONF_ELECTRICITY_VAT = "electricity_vat"
CONF_EXCLUDE_FROM_RECORDING = "exclude_from_recording"

# Solar forecast configuration
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_POWER_ENTITY = "power_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"

# Battery charge mode configuration
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_MAX_CHARGE_POWER_W = "battery_max_charge_power_w"
CONF_BATTERY_DEGRADATION_COST = "battery_degradation_cost"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_CHARGE_POWER_ENTITY = "battery_charge_power_entity"
CONF_GRID_IMPORT_ENTITY = "grid_import_entity"
CONF_GRID_EXPORT_ENTITY = "grid_export_entity"
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
CONF_POWER_METER_CONSUMPTION = "power_meter_consumption"
CONF_WATER_HEATER_ACTIVE_ENTITY = "water_heater_active_entity"
CONF_CENTRAL_HEATING_ACTIVE_ENTITY = "central_heating_active_entity"
CONF_WATER_HEATER_POWER_ENTITY = "water_heater_power_entity"
CONF_WATER_HEATER_POWER_W = "water_heater_power_w"
CONF_WATER_HEATER_MAX_HOURS = "water_heater_max_hours"
CONF_BATHROOM_HUMIDITY_ENTITY = "bathroom_humidity_entity"
CONF_POOL_PUMP_POWER_ENTITY = "pool_pump_power_entity"
CONF_POOL_PUMP_POWER_W = "pool_pump_power_w"
CONF_DEHUMIDIFIER_POWER_ENTITY = "dehumidifier_power_entity"
CONF_DEHUMIDIFIER_POWER_W = "dehumidifier_power_w"

# Solar forecast sensor attributes
ATTR_FORECASTS = "forecasts"
ATTR_TOTAL_SAMPLES = "total_samples"
ATTR_DATA_SINCE = "data_since"
ATTR_ENERGY_TODAY_KWH = "energy_today_kwh"
ATTR_ENERGY_TOMORROW_KWH = "energy_tomorrow_kwh"
ATTR_INTRADAY_SCALING = "intraday_scaling"

PREFERRED_SENSOR_ENTITY_IDS: dict[str, str] = {
    "electricitypricelevels": "sensor.energy_advisor_price",
    "compactlevels": "sensor.energy_advisor_compact_levels",
    "batterychargemode": "sensor.energy_advisor_battery_charge_mode",
    "solarforecast": "sensor.energy_advisor_solar_forecast",
    "base_load": "sensor.energy_advisor_base_load",
    "strategy": "sensor.energy_advisor_strategy",
    "battery_floor": "sensor.energy_advisor_battery_floor",
    "battery_floor_pct": "sensor.energy_advisor_battery_floor_pct",
    "learning_nights": "sensor.energy_advisor_learning_nights",
    "battery_soc_forecast": "sensor.energy_advisor_battery_soc_forecast",
}


def build_sensor_unique_id(entry: ConfigEntry, key: str) -> str:
    """Build a stable sensor unique ID that survives config-entry recreation."""
    stable_prefix = getattr(entry, "unique_id", None)
    if not isinstance(stable_prefix, str) or not stable_prefix:
        stable_prefix = entry.entry_id
    return f"{stable_prefix}_{key}"


def parse_unit_of_measurement(unit_str: str) -> tuple[str | None, str | None]:
    """
    Parse a unit of measurement string into currency and energy unit.

    Expected formats:
    - "SEK/kWh" -> ("SEK", "kWh")
    - "EUR/MWh" -> ("EUR", "MWh")
    - "NOK/kWh" -> ("NOK", "kWh")
    - "" -> (None, None)

    Returns:
        A tuple of (currency, energy_unit)
    """
    if not unit_str or not isinstance(unit_str, str):
        return None, None

    unit_str = unit_str.strip()

    if "/" in unit_str:
        parts = unit_str.split("/")
        if len(parts) == 2:
            currency = parts[0].strip() if parts[0].strip() else None
            energy_unit = parts[1].strip() if parts[1].strip() else None
            return currency, energy_unit
        return None, None

    common_energy_units = {"kwh", "mwh", "wh", "kw", "mw", "w"}

    if unit_str.lower() in common_energy_units:
        return None, unit_str
    elif len(unit_str) == 3 and unit_str.isupper():
        return unit_str, None
    else:
        if any(unit_str.lower().endswith(u) for u in common_energy_units):
            return None, unit_str
        return unit_str, None
