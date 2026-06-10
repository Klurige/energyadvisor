"""Constants for the electricitypricelevels integration."""

from __future__ import annotations

import logging

DOMAIN = "electricitypricelevels"
LOGGER = logging.getLogger(__package__)

# Development configuration — imported from dev_config.py (gitignored).
# If the file is missing, dev features are disabled with safe defaults.
try:
    from .dev_config import DEV_DEFAULTS, DEV_DEFAULTS_ENABLED, HA_URL, HA_TOKEN  # noqa: F401
except ImportError:
    DEV_DEFAULTS_ENABLED = False
    DEV_DEFAULTS: dict = {}  # type: ignore[no-redef]
    HA_URL = ""
    HA_TOKEN = ""

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
