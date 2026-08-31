import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor.const import CONF_EXCLUDE_FROM_RECORDING
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)

TEST_TIMEZONE = ZoneInfo("Europe/Helsinki")


def _build_sensor(now: datetime.datetime) -> BatteryChargeModeSensor:
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.time_zone = "Europe/Helsinki"

    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = "test_unique_id"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: True}

    device_info = MagicMock(spec=DeviceInfo)

    price_sensor = MagicMock()
    price_sensor.entity_id = "sensor.price"
    price_sensor.state = "1.00"
    price_sensor._rates = [
        {
            "start": datetime.datetime(
                2026, 8, 15, 21, 15, tzinfo=datetime.timezone.utc
            ),
            "cost": 0.1,
        },
        {
            "start": datetime.datetime(
                2026, 8, 15, 21, 30, tzinfo=datetime.timezone.utc
            ),
            "cost": 0.2,
        },
    ]

    with patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
        return_value=now,
    ):
        return BatteryChargeModeSensor(hass, entry, device_info, price_sensor)


def test_calculate_battery_mode_formats_rate_start_in_local_time():
    now = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor(now)

    with patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
        return_value=now,
    ), patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.as_local",
        side_effect=lambda dt: dt.astimezone(TEST_TIMEZONE),
    ):
        sensor.calculate_battery_mode()

    assert sensor.state == "maxuse"
    assert [mode["from"] for mode in sensor._modes] == [
        "2026-08-15T00:00",
        "2026-08-16T00:15",
    ]
