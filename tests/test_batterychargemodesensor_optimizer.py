import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_MAX_SOC_PCT,
    CONF_BATTERY_MIN_SOC_PCT,
    CONF_BATTERY_OPTIMIZATION_ENABLED,
    CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
)
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)


TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")


def _build_sensor(now: datetime.datetime) -> BatteryChargeModeSensor:
    """Create a battery sensor backed by a lightweight price and SoC stub."""
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.time_zone = "Europe/Stockholm"

    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = "test_unique_id"
    entry.options = {
        CONF_EXCLUDE_FROM_RECORDING: True,
        CONF_BATTERY_CAPACITY_KWH: 10.0,
        CONF_BATTERY_MAX_CHARGE_POWER_W: 10000.0,
        CONF_BATTERY_MAX_DISCHARGE_POWER_W: 10000.0,
        CONF_BATTERY_OPTIMIZATION_ENABLED: True,
        CONF_BATTERY_OPTIMIZATION_HORIZON_HOURS: 2.0,
        CONF_BATTERY_MIN_SOC_PCT: 20.0,
        CONF_BATTERY_MAX_SOC_PCT: 80.0,
        CONF_BATTERY_SOC_ENTITY: "sensor.battery_soc",
    }

    device_info = MagicMock(spec=DeviceInfo)

    price_sensor = MagicMock()
    price_sensor.entity_id = "sensor.price"
    price_sensor.state = "0.10"
    price_sensor._rates = [
        {
            "start": now,
            "end": now + datetime.timedelta(hours=1),
            "cost": 0.10,
            "credit": 0.05,
        },
        {
            "start": now + datetime.timedelta(hours=1),
            "end": now + datetime.timedelta(hours=2),
            "cost": 0.60,
            "credit": 0.90,
        },
    ]

    soc_state = MagicMock()
    soc_state.state = "50"
    soc_state.attributes = {}
    hass.states = MagicMock()
    hass.states.get.return_value = soc_state

    with (
        patch(
            "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.energyadvisor.sensor.chargemodehelpers.dt_util.now",
            return_value=now,
        ),
    ):
        return BatteryChargeModeSensor(hass, entry, device_info, price_sensor)


def test_calculate_battery_mode_uses_optimizer_and_exposes_schedule() -> None:
    """The sensor should expose the optimized schedule when enabled."""
    now = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor(now)

    with (
        patch(
            "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.energyadvisor.sensor.chargemodehelpers.dt_util.now",
            return_value=now,
        ),
    ):
        sensor.calculate_battery_mode()

    attrs = sensor.extra_state_attributes

    assert sensor.state == "charge"
    assert attrs["optimization_enabled"] is True
    assert attrs["current_soc_pct"] == 50.0
    assert attrs["current_target_soc"] == 80.0
    assert attrs["reason"].startswith("Optimized 2h price schedule with HiGHS")
    assert attrs["solver"] == "HIGHS"
    assert [entry["mode"] for entry in attrs["charge_entries"]] == [
        "charge",
        "sell",
    ]
    assert attrs["charge_entries"][0]["target_soc"] == 80.0
    assert attrs["charge_entries"][1]["target_soc"] == 20.0
    assert attrs["modes"] == attrs["charge_entries"]
