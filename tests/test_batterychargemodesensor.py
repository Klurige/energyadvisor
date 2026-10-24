import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor.const import CONF_EXCLUDE_FROM_RECORDING
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)

TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")


def _slot(
    start: datetime.datetime, minutes: int, cost: float = 1.0, credit: float = 0.5
) -> dict:
    return {
        "start": start,
        "end": start + datetime.timedelta(minutes=minutes),
        "cost": cost,
        "credit": credit,
    }


def _quarter_hour_rates() -> list[dict]:
    first = datetime.datetime(2026, 8, 15, 23, 45, tzinfo=TEST_TIMEZONE)
    return [
        _slot(first + datetime.timedelta(minutes=15 * i), 15, 1.0 + i, 0.25 * i)
        for i in range(4)
    ]


def _build_sensor(rates: list[dict]) -> BatteryChargeModeSensor:
    hass = MagicMock()
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = "test_unique_id"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: True}

    price_sensor = MagicMock()
    price_sensor._rates = rates
    price_sensor.async_add_update_listener.return_value = MagicMock()

    sensor = BatteryChargeModeSensor(
        hass, entry, MagicMock(spec=DeviceInfo), price_sensor
    )
    sensor.async_write_ha_state = MagicMock()
    return sensor


def test_modes_are_standby_for_every_price_slot_with_matching_times() -> None:
    sensor = _build_sensor(_quarter_hour_rates())

    modes = sensor._build_modes()

    assert [(m["from"], m["to"]) for m in modes] == [
        ("2026-08-15T23:45", "2026-08-16T00:00"),
        ("2026-08-16T00:00", "2026-08-16T00:15"),
        ("2026-08-16T00:15", "2026-08-16T00:30"),
        ("2026-08-16T00:30", "2026-08-16T00:45"),
    ]
    assert {m["mode"] for m in modes} == {"standby"}


def test_modes_carry_cost_and_credit_from_the_price_slots_and_target_soc_50() -> None:
    sensor = _build_sensor(_quarter_hour_rates())

    modes = sensor._build_modes()

    assert [m["cost"] for m in modes] == [1.0, 2.0, 3.0, 4.0]
    assert [m["credit"] for m in modes] == [0.0, 0.25, 0.5, 0.75]
    assert {m["target_soc"] for m in modes} == {50}


def test_modes_round_cost_and_credit_to_two_decimals() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor([_slot(start, 15, cost=1.23456, credit=0.98765)])

    assert sensor._build_modes() == [
        {
            "from": "2026-08-15T00:00",
            "to": "2026-08-15T00:15",
            "mode": "standby",
            "target_soc": 50,
            "cost": 1.23,
            "credit": 0.99,
        }
    ]


def test_modes_follow_slot_length_of_the_price_slots() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor(
        [_slot(start, 60), _slot(start + datetime.timedelta(hours=1), 60)]
    )

    assert [(m["from"], m["to"]) for m in sensor._build_modes()] == [
        ("2026-08-15T00:00", "2026-08-15T01:00"),
        ("2026-08-15T01:00", "2026-08-15T02:00"),
    ]


def test_modes_are_empty_without_price_data() -> None:
    sensor = _build_sensor([])

    assert sensor._build_modes() == []
    assert sensor.state == "standby"
    assert sensor.extra_state_attributes == {"modes": []}


def test_slots_without_times_are_skipped() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor([{"cost": 1.0}, _slot(start, 15)])

    assert len(sensor._build_modes()) == 1


async def test_added_to_hass_builds_modes_and_listens_to_price_updates() -> None:
    sensor = _build_sensor(_quarter_hour_rates())
    sensor.async_on_remove = MagicMock()

    await sensor.async_added_to_hass()

    assert len(sensor.extra_state_attributes["modes"]) == 4
    sensor._price_sensor.async_add_update_listener.assert_called_once_with(
        sensor._handle_price_update
    )
    sensor.async_on_remove.assert_called_once_with(
        sensor._price_sensor.async_add_update_listener.return_value
    )


def test_price_update_rebuilds_modes_and_writes_state() -> None:
    sensor = _build_sensor(_quarter_hour_rates())
    assert sensor.extra_state_attributes["modes"] == []

    sensor._handle_price_update()
    assert len(sensor.extra_state_attributes["modes"]) == 4
    sensor.async_write_ha_state.assert_called_once()

    sensor._price_sensor._rates = _quarter_hour_rates()[:2]
    sensor._handle_price_update()
    assert len(sensor.extra_state_attributes["modes"]) == 2
    assert sensor.state == "standby"


def test_modes_attribute_is_unrecorded() -> None:
    sensor = _build_sensor([])

    assert "modes" in sensor._unrecorded_attributes
