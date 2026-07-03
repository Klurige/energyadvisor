"""Constants for the Counter Demo integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

DOMAIN = "counter_demo"

PREFERRED_SENSOR_ENTITY_IDS: dict[str, str] = {
    "cycle_counter": "sensor.counter_demo_cycle_counter",
}


def build_sensor_unique_id(entry: ConfigEntry, key: str) -> str:
    """Build a stable sensor unique ID."""
    stable_prefix = getattr(entry, "unique_id", None)
    if not isinstance(stable_prefix, str) or not stable_prefix:
        stable_prefix = entry.entry_id
    return f"{stable_prefix}_{key}"
