"""Tests for the Counter Demo sensor."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.counter_demo.const import DOMAIN
from custom_components.counter_demo.sensor import CounterDemoSensor, async_setup_entry

UTC = timezone.utc


@pytest.fixture
def hass():
    h = MagicMock()
    h.config = MagicMock()
    h.config.time_zone = "UTC"
    return h


@pytest.fixture
def entry():
    e = MagicMock()
    e.entry_id = "test_entry_id"
    e.options = {}
    return e


@pytest.fixture
def device_info():
    return MagicMock()


def test_constructor_uses_preferred_entity_id(hass, entry, device_info):
    sensor = CounterDemoSensor(hass, entry, device_info)

    assert sensor.entity_id == "sensor.counter_demo_cycle_counter"
    assert sensor.native_value == 0


@pytest.mark.asyncio
async def test_async_setup_entry_adds_sensor_entity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    added_entities = []

    def _add_entities(entities, update_before_add=False):
        added_entities.extend(entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(added_entities) == 1
    assert isinstance(added_entities[0], CounterDemoSensor)


def test_cycle_counter_counts_up_then_down(hass, entry, device_info):
    sensor = CounterDemoSensor(hass, entry, device_info)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    now = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)

    for _ in range(100):
        sensor._handle_tick(now)
    assert sensor.native_value == 100

    sensor._handle_tick(now)
    assert sensor.native_value == 99

    for _ in range(99):
        sensor._handle_tick(now)
    assert sensor.native_value == 0
    assert sensor.async_write_ha_state.call_count == 200


@pytest.mark.asyncio
async def test_async_added_to_hass_registers_one_second_timer(hass, entry, device_info):
    sensor = CounterDemoSensor(hass, entry, device_info)
    sensor.hass = hass

    with patch(
        "custom_components.counter_demo.sensor.async_track_time_interval",
        return_value=lambda: None,
    ) as mock_track:
        await sensor.async_added_to_hass()

    mock_track.assert_called_once()
